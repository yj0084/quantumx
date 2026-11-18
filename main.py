import os
import sys

try:
    sys.stdout.reconfigure(line_buffering=True)  # 로그 즉시 출력
except Exception:
    pass

import json
import re
import html as html_mod
import requests
import pandas as pd
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
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
                                "accumulatedTradingVolume": "volume",
                                "highPrice": "high",
                                "lowPrice": "low"})
        df["close"] = df["close"].map(_num)
        df["volume"] = df["volume"].map(_num)
        keep = ["date", "close", "volume"]
        # 매물대(Volume Profile)는 봉별 고·저 범위가 있으면 더 정확하다. 없으면 종가로 대체.
        for col in ("high", "low"):
            if col in df.columns:
                df[col] = df[col].map(_num)
                keep.append(col)
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
        df = df[keep].dropna(subset=["date", "close"])
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
        highs = quote.get("high") or []
        lows = quote.get("low") or []

        rows = []
        for i in range(min(len(stamps), len(closes))):
            close = _num(closes[i])
            if close is None or close <= 0:
                continue
            vol = _num(volumes[i]) if i < len(volumes) else None
            high = _num(highs[i]) if i < len(highs) else None
            low = _num(lows[i]) if i < len(lows) else None
            rows.append({"date": datetime.fromtimestamp(stamps[i]),
                         "close": close,
                         "high": high if high else close,
                         "low": low if low else close,
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
    "핵심매물대": ("최근 최대 120거래일 거래량을 가격대별로 쌓아 가장 두꺼운 구간을 최대 매물대(POC)로 보고, "
               "그 구간과 붙지 않은(±1구간 밖) 곳에서 그다음으로 두꺼운 2~3개 구간을 '매물 집중 구간'으로 보여준다. "
               "비중(%) = 그 구간에 쌓인 거래량 ÷ 전체 거래량 × 100. 현재가 대비 거리(%)는 "
               "(구간 중심가 - 현재가) ÷ 현재가 × 100이라 양수면 현재가 위(저항 후보), 음수면 아래(지지 후보)다. "
               "거래량이 많이 바뀐 자리는 그 가격에서 사고팔린 사람이 많다는 뜻이라 지지·저항 후보가 되지만 절대적인 벽은 아니다."),
    "20일 VWAP (보조)": ("보조 지표. VWAP20 = Σ(종가×거래량) ÷ Σ(거래량) (최근 20거래일). 거래량 가중평균단가로 "
                   "'최근 20거래일 시장 참여자의 평균 매입단가' 추정치다. 화면에서는 1번 카드 아래 작은 글씨로만 쓰고, "
                   "축 점수의 과열(추격) 감점 항에만 사용한다. 비교용으로 60거래일 VWAP도 함께 낸다."),
    "VWAP괴리율": ("(현재가 - 20일 VWAP) ÷ 20일 VWAP × 100. 양수면 현재가가 평균 매입단가보다 높아 추격 부담, "
                "음수면 낮아 평균 매수자 손실 구간으로 본다. 매물대 거리와 달리 점수 계산에만 쓰인다."),
    "RSI14": ("Wilder 방식. 14거래일 평균상승폭(AU)과 평균하락폭(AD)을 지수평활로 갱신하고 "
              "RS=AU/AD, RSI=100-100/(1+RS). 30 이하 과매도, 70 이상 과매수."),
    "ROC": ("(현재가 ÷ N거래일 전 종가 - 1) × 100. 5일=단기 모멘텀, 20일=중기 모멘텀."),
    "거래량모멘텀": "최근 5일 평균 거래량 ÷ 최근 20일 평균 거래량. 1 초과면 최근 거래가 평소보다 활발하다.",
    "수급": ("한국투자증권 inquire-investor(FHKST01010900)의 일별 외국인·기관 순매수 수량을 5·20거래일 누적한 값. "
            "비중(%) = 누적 순매수 수량 ÷ 같은 기간 누적 거래량 × 100. 미국 종목은 이 API가 없어 미제공."),
    "이평선": ("MA5/MA20/MA60 = 최근 N거래일 종가 단순평균. 현재가가 이평선 위면 지지, 아래면 이탈로 본다. "
             "MA5>MA20>MA60이면 정배열(상승 추세), 반대면 역배열."),
    "축별점수_매집": ("매집점수 = 50 + (매집비-50)×0.8 - (20일 VWAP 괴리율×1.2), 괴리율은 ±15%로 제한 후 0~100 클리핑. "
                 "괴리율 항은 평균 매입단가 대비 과열(추격) 정도를 빼는 항이고, 매물대(POC)는 점수가 아니라 위치 판정에 쓴다."),
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
    "지지저항": ("지지선 = 현재가 아래에 있는 값들 중 가까운 순 최대 5개, 저항선 = 현재가 위에 있는 값들 중 "
             "가까운 순 최대 5개. 후보는 ①이동평균(MA5·MA20·MA60) ②20일 VWAP ③최근 20·60일 종가 최저/최고 "
             "④매물대(Volume Profile): 최근 최대 120거래일 가격 범위를 18개 구간으로 나눠 봉별 거래량을 "
             "그 봉이 걸친 구간에 균등 배분해 누적한 뒤 최대 구간(POC, 최대 매물대)과 2순위 구간(주요 매물대)의 "
             "중심가 ⑤거래량 급증봉: 직전 20일 평균 거래량의 2배 이상 터진 봉 중 최근 발생 순 최대 3개의 "
             "고·저 중심가(고·저가 없으면 종가). 0.25% 이내로 겹치는 값은 매물대·급증봉 이름을 남기고 한 줄로 합친다."),
    "매물대": ("Volume Profile. 최근 최대 120거래일 일봉의 가격 범위(고가 최대~저가 최소)를 18개 균등 구간으로 "
             "나누고, 각 봉의 거래량을 그 봉의 고가~저가가 걸친 구간들에 똑같이 나눠 누적한다. 누적 거래량이 가장 큰 "
             "구간의 중심가가 POC(최대 매물대), POC와 붙지 않은 구간 중 다음으로 큰 곳이 2차 매물대다. "
             "매물대는 그 가격에서 많이 거래됐다는 뜻이라 지지·저항 후보로 쓰지만, 절대적인 벽은 아니다."),
    "거래량급증봉": ("직전 20거래일 평균 거래량 대비 2.0배 이상 거래량이 발생한 봉을 최근 60거래일 안에서 최근 발생 순 "
                 "최대 3개 찾고, 서로 1% 안에 붙은 봉은 하나만 남긴다. 기준가는 그 봉의 고·저 중심가(고·저가 없으면 종가). "
                 "거래량이 급증한 자리는 매물이 쌓인 자리로 보고 지지·저항 후보에 넣는다."),
    "증권사리포트": ("네이버 증권 리서치(모바일)의 종목별 최신 리포트 최대 5건. 증권사·작성일·제목·투자의견·목표주가와 "
                "본문 앞 1~2문장 요약, PDF 원문 링크를 제공한다. 컨센서스의 평균·최저·최고 목표주가는 그 표본들의 단순 "
                "평균·범위이며, 증권사 전체 컨센서스가 아니라 최근 리포트 표본 요약이다."),
    "애널리스트컨센서스": ("미국 종목은 야후 파이낸스 애널리스트 데이터(목표주가 최저·평균·중앙·최고, 최근 1개월 투자의견 "
                    "분포, 투자의견 평균점수, 애널리스트 수)를 쓴다. 데이터가 없거나 응답이 실패하면 미제공으로 표시한다."),
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


# ==========================
# 📊 매물대(Volume Profile) · 거래량 급증봉
# ==========================
def _price_range_series(df):
    """봉별 (저가, 고가). 일봉 고·저가가 없으면 종가로 대체한다."""
    close = df["close"].astype(float)
    low = df["low"].astype(float) if "low" in df.columns else close
    high = df["high"].astype(float) if "high" in df.columns else close
    low = low.where(low.notna() & (low > 0), close)
    high = high.where(high.notna() & (high > 0), close)
    return pd.concat([low, close], axis=1).min(axis=1), pd.concat([high, close], axis=1).max(axis=1)


def calc_volume_profile(df, buckets=18, lookback=120, top_n=3, min_share=2.0):
    """가격대별 매물대. 조회 이력의 가격 범위를 buckets개 구간으로 나눠 각 봉의 거래량을
    그 봉이 걸친 구간들에 균등 배분해 누적하고, 최대 구간(POC)과 매물 집중 구간 TOP N을 뽑는다."""
    if df is None or df.empty or len(df) < 20:
        return None
    try:
        tail = df.tail(lookback)
        low_s, high_s = _price_range_series(tail)
        vols = tail["volume"].astype(float).fillna(0.0)
        if float(vols.sum()) <= 0:
            return None
        lo, hi = float(low_s.min()), float(high_s.max())
        if not (lo > 0 and hi > lo):
            return None
        step = (hi - lo) / buckets
        if step <= 0:
            return None

        acc = [0.0] * buckets
        for l, h, v in zip(low_s.tolist(), high_s.tolist(), vols.tolist()):
            if not v or v <= 0:
                continue
            b1 = min(buckets - 1, max(0, int((float(l) - lo) / step)))
            b2 = min(buckets - 1, max(0, int((float(h) - lo) / step)))
            if b2 < b1:
                b1, b2 = b2, b1
            share = v / (b2 - b1 + 1)
            for b in range(b1, b2 + 1):
                acc[b] += share

        total = sum(acc)
        if total <= 0:
            return None
        centers = [lo + step * (i + 0.5) for i in range(buckets)]
        poc_i = max(range(buckets), key=lambda i: acc[i])

        # 매물 집중 구간: 누적 거래량이 큰 구간부터. POC와 붙어있는 구간(±1)은 같은 매물대로
        # 보고 건너뛰고, 2순위부터는 비중이 min_share% 미만이면 넣지 않는다.
        clusters = []
        for i in sorted(range(buckets), key=lambda i: -acc[i]):
            if acc[i] <= 0 or len(clusters) >= top_n:
                break
            if any(abs(i - c["구간"]) <= 1 for c in clusters):
                continue
            share = acc[i] / total * 100
            if clusters and share < min_share:
                continue
            clusters.append({
                "순위": len(clusters) + 1,
                "구간": i,
                "하한": round(lo + step * i, 2),
                "상한": round(lo + step * (i + 1), 2),
                "중심가": round(centers[i], 2),
                "비중_pct": round(share, 2),
            })
        second_i = clusters[1]["구간"] if len(clusters) > 1 else None
        return {
            "구간수": buckets,
            "조회일수": int(len(tail)),
            "가격하한": round(lo, 2),
            "가격상한": round(hi, 2),
            "구간폭": round(step, 2),
            "POC": round(centers[poc_i], 2),
            "POC_비중_pct": round(acc[poc_i] / total * 100, 2),
            "2차매물대": (round(centers[second_i], 2) if second_i is not None else None),
            "2차_비중_pct": (round(acc[second_i] / total * 100, 2) if second_i is not None else None),
            # 매물 집중 구간 TOP N (1순위가 POC). 중심가·가격 구간·거래 비중을 함께 준다.
            "매물집중구간": clusters,
            "상위구간_합산_비중_pct": round(sum(c["비중_pct"] for c in clusters), 2),
        }
    except Exception as e:
        print("⚠️ 매물대 계산 오류:", e)
        return None


def calc_volume_spikes(df, multiple=2.0, lookback=60, base_window=20, top=3):
    """직전 base_window일 평균 거래량 대비 multiple배 이상 터진 봉을 최근 발생 순으로 최대 top개.
    핵심 기준가는 고·저 중심가(없으면 종가)."""
    if df is None or df.empty or len(df) < base_window + 5:
        return []
    try:
        d = df.copy()
        d["vol"] = d["volume"].astype(float).fillna(0.0)
        d["base"] = d["vol"].rolling(base_window).mean().shift(1)
        tail = d.tail(lookback)
        found = []
        for i in range(len(tail) - 1, -1, -1):
            row = tail.iloc[i]
            base = row["base"]
            if pd.isna(base) or base <= 0 or row["vol"] <= 0:
                continue
            ratio = float(row["vol"]) / float(base)
            if ratio < multiple:
                continue
            low = row["low"] if "low" in tail.columns and not pd.isna(row["low"]) else None
            high = row["high"] if "high" in tail.columns and not pd.isna(row["high"]) else None
            px = (float(low) + float(high)) / 2 if (low and high) else float(row["close"])
            found.append({"날짜": row["date"].strftime("%m/%d"),
                          "일자": row["date"].strftime("%Y-%m-%d"),
                          "가격": round(px, 2),
                          "배수": round(ratio, 2)})
            if len(found) >= top:
                break
        # 서로 1% 안에 붙은 봉은 같은 자리로 보고 가장 최근 것만 남긴다.
        out = []
        for s in found:
            if any(abs(s["가격"] - o["가격"]) / o["가격"] * 100 < 1.0 for o in out):
                continue
            out.append(s)
        return out
    except Exception as e:
        print("⚠️ 거래량 급증봉 계산 오류:", e)
        return []


# ==========================
# 📄 증권사 리포트(국내) · 애널리스트 컨센서스(미국)
# ==========================
_NAVER_RESEARCH_LIST = "https://m.stock.naver.com/front-api/research/stockList"
_NAVER_RESEARCH_PAGE = "https://m.stock.naver.com/investment/research/company/{rid}"
_RESEARCH_HDRS = {"User-Agent": UA, "Referer": "https://m.stock.naver.com/"}
_RESEARCH_CACHE = {}
_RESEARCH_CACHE_TTL = 1800
_RESEARCH_EMPTY_TTL = 180

_OPINION_KO = {
    "strong buy": "Strong Buy", "strongbuy": "Strong Buy", "적극매수": "Strong Buy", "적극 매수": "Strong Buy",
    "trading buy": "Trading Buy", "buy": "Buy", "매수": "Buy",
    "marketperform": "Hold", "market perform": "Hold", "hold": "Hold", "중립": "Hold", "보유": "Hold",
    "underperform": "Underperform", "reduce": "Reduce", "sell": "Sell", "매도": "Sell", "축소": "Reduce",
}


def _strip_html(text):
    s = str(text or "")
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"</(p|div|tr|li)>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html_mod.unescape(s)
    s = re.sub(r"[ \t\u00a0]+", " ", s)
    s = re.sub(r"\n{2,}", "\n", s)
    return s.strip()


def _summary_sentences(text, max_sentences=2, max_chars=230):
    """본문 앞 1~2문장만 남긴 요약."""
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    if not flat:
        return ""
    parts = re.split(r"(?<=[.!?])\s+", flat)
    out = ""
    for p in parts[:max_sentences]:
        out = (out + " " + p).strip()
    if not out:
        out = flat[:max_chars]
    if len(out) > max_chars:
        out = out[:max_chars].rstrip() + "…"
    return out


def _pick_summary(body, preview=""):
    """본문이 있으면 앞부분 1~2문장, 없으면 목록 미리보기 문장을 쓴다.
    본문 첫 줄이 제목·부제 같은 짧은 머리말이면 건너뛰고 다음 문장부터 요약한다."""
    text = str(body or "").strip()
    if text:
        paras = [p.strip() for p in text.split("\n") if p.strip()]
        if len(paras) > 1 and len(paras[0]) < 70 and not paras[0].endswith((".", "다", "요", "…")):
            paras = paras[1:]
        s = _summary_sentences(paras[0] if paras else text)
        if s:
            return s
    return _summary_sentences(preview)


def _parse_opinion(text):
    t = str(text or "").lower()
    if not t:
        return None
    # 긴 영문 표기부터 확인해 'buy'가 'strong buy'를 잘못 집지 않게 한다.
    for key in ("strong buy", "strongbuy", "trading buy", "marketperform", "market perform",
                "underperform", "buy", "hold", "중립", "보유", "매수", "매도", "sell", "reduce", "축소", "적극매수"):
        if key.isascii():
            if re.search(r"(?<![a-z])" + re.escape(key) + r"(?![a-z])", t):
                return _OPINION_KO.get(key)
        elif key in t:
            return _OPINION_KO.get(key)
    return None


def _parse_target_price(text):
    """본문 문장에서 목표주가를 원 단위로 추출. 예: '목표주가 37만원', '목표주가를 40만원으로', '목표주가 370,000원'."""
    t = str(text or "")
    for pat in (r"목표\s*주가[^0-9]{0,14}([0-9][0-9,\.]*)\s*(만원|원)?",
                r"\bTP\b[^0-9]{0,6}([0-9][0-9,\.]*)\s*(만원|원)?"):
        m = re.search(pat, t)
        if not m:
            continue
        try:
            num = float(m.group(1).replace(",", ""))
        except (TypeError, ValueError):
            continue
        unit = m.group(2) or "원"
        val = num * 10000 if unit == "만원" else num
        if 100 <= val <= 5_000_000:
            return round(val)
    return None


def _kr_report_detail(research_id):
    """네이버 증권 리포트 상세 페이지의 __NEXT_DATA__에서 의견·목표주가·본문·PDF 링크를 뽑는다."""
    try:
        res = requests.get(_NAVER_RESEARCH_PAGE.format(rid=research_id),
                           headers=_RESEARCH_HDRS, timeout=4)
        if res.status_code != 200:
            return {}
        m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                      res.text, re.S)
        if not m:
            return {}
        data = json.loads(m.group(1))
        queries = (((data.get("props") or {}).get("pageProps") or {})
                   .get("dehydratedState") or {}).get("queries") or []
        for q in queries:
            result = ((q.get("state") or {}).get("data") or {}).get("result")
            if isinstance(result, dict) and result.get("researchContent"):
                return result["researchContent"]
    except Exception as e:
        print(f"⚠️ 리포트 상세 파싱 오류({research_id}):", type(e).__name__)
    return {}


def get_kr_research_reports(code, limit=5, price=None):
    """국내 종목 최신 증권사 리포트 목록 + 의견·목표주가·요약·PDF 링크. 실패해도 빈 결과만 돌려준다."""
    key = f"{code}:{limit}"
    hit = _RESEARCH_CACHE.get(key)
    if hit and (datetime.now() - hit["at"]).total_seconds() < hit["ttl"]:
        return hit["data"]

    items = []
    try:
        res = requests.get(_NAVER_RESEARCH_LIST,
                           params={"itemCode": code, "page": 1, "pageSize": limit},
                           headers=_RESEARCH_HDRS, timeout=4)
        if res.status_code == 200:
            raw = (res.json() or {}).get("result") or []
            items = [r for r in raw if str(r.get("itemCode")) == str(code)][:limit]
    except Exception as e:
        print("⚠️ 증권사 리포트 목록 오류:", type(e).__name__)
        items = []

    if not items:
        data = {"지원": True, "상태": "empty", "통화": "KRW", "리포트": [], "컨센서스": None,
                "비고": "최근 증권사 리포트를 찾지 못했습니다(네이버 증권 리서치 기준)."}
        _RESEARCH_CACHE[key] = {"at": datetime.now(), "ttl": _RESEARCH_EMPTY_TTL, "data": data}
        return data

    details = {}
    try:
        with ThreadPoolExecutor(max_workers=min(5, len(items))) as pool:
            futures = {pool.submit(_kr_report_detail, it.get("researchId")): it.get("researchId")
                       for it in items}
            for fut, rid in futures.items():
                try:
                    details[rid] = fut.result(timeout=8)
                except Exception:
                    details[rid] = {}
    except Exception as e:
        print("⚠️ 리포트 상세 병렬 조회 오류:", type(e).__name__)

    reports, targets, opinions = [], [], {}
    for it in items:
        rid = it.get("researchId")
        det = details.get(rid) or {}
        body = _strip_html(det.get("content") or "")
        summary = _pick_summary(body, it.get("previewContent") or "")

        goal = det.get("goalPrice")
        try:
            goal = float(str(goal).replace(",", "")) if goal not in (None, "") else None
        except (TypeError, ValueError):
            goal = None
        if not goal:
            goal = _parse_target_price(it.get("previewContent") or body)
        opinion = _parse_opinion(det.get("opinion")) or _parse_opinion(it.get("title") or "") \
            or _parse_opinion(it.get("previewContent") or "")

        write_date = str(it.get("writeDate") or "")
        rep = {
            "증권사": str(det.get("brokerName") or it.get("brokerName") or "").strip(),
            "작성일": write_date,
            "제목": str(it.get("title") or "").strip(),
            "투자의견": opinion,
            "목표주가": goal,
            "목표주가_표시": (_fmt_price(goal, "KRW") if goal else None),
            "현재가_대비_pct": (round((goal / price - 1) * 100, 2) if goal and price else None),
            "요약": summary,
            "원문_PDF": det.get("attachUrl") or None,
            "상세": f"https://m.stock.naver.com/investment/research/company/{rid}",
        }
        reports.append(rep)
        if goal:
            targets.append(float(goal))
        if opinion:
            opinions[opinion] = opinions.get(opinion, 0) + 1

    consensus = None
    if targets:
        consensus = {
            "기준": f"최근 리포트 {len(targets)}건 표본",
            "표본수": len(targets),
            "평균_목표주가": round(sum(targets) / len(targets), 0),
            "최저_목표주가": round(min(targets), 0),
            "최고_목표주가": round(max(targets), 0),
            "현재가": round(price, 2) if price else None,
            "평균_괴리율_pct": (round((sum(targets) / len(targets) / price - 1) * 100, 2) if price else None),
            "투자의견_분포": opinions,
        }

    data = {"지원": True, "상태": "ok", "통화": "KRW", "출처": "네이버 증권 리서치",
            "리포트": reports, "컨센서스": consensus}
    _RESEARCH_CACHE[key] = {"at": datetime.now(), "ttl": _RESEARCH_CACHE_TTL, "data": data}
    return data


_YAHOO_HTTP = {"session": None, "crumb": None, "at": None}


def _yahoo_crumb(force=False):
    """야후 quoteSummary는 쿠키+crumb가 필요하다. 10분 캐시.
    서버 IP에 따라 fc.yahoo.com이 쿠키를 안 주는 경우가 있어 다른 입구도 시도한다."""
    now = datetime.now()
    if (not force and _YAHOO_HTTP.get("crumb")
            and _YAHOO_HTTP.get("at") and (now - _YAHOO_HTTP["at"]).total_seconds() < 600):
        return _YAHOO_HTTP["crumb"]
    try:
        if _YAHOO_HTTP.get("session") is None:
            _YAHOO_HTTP["session"] = requests.Session()
        sess = _YAHOO_HTTP["session"]
        headers = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
                   "Accept-Language": "en-US,en;q=0.9"}
        for warm in ("https://fc.yahoo.com", "https://finance.yahoo.com/",
                     "https://finance.yahoo.com/quote/SPY"):
            try:
                sess.get(warm, headers=headers, timeout=4)
            except Exception:
                pass
            if sess.cookies:
                break
        for host in ("query1", "query2"):
            res = sess.get(f"https://{host}.finance.yahoo.com/v1/test/getcrumb",
                           headers=headers, timeout=4)
            crumb = (res.text or "").strip()
            if res.status_code == 200 and crumb and "<" not in crumb:
                _YAHOO_HTTP.update({"crumb": crumb, "at": now})
                return crumb
    except Exception as e:
        print("⚠️ 야후 crumb 오류:", type(e).__name__)
    return None


def _stockanalysis_consensus(ticker, price=None):
    """야후가 서버 IP를 막을 때 쓰는 대체 소스: stockanalysis.com(S&P Global 애널리스트 집계) 문장 파싱."""
    try:
        res = requests.get(f"https://stockanalysis.com/stocks/{str(ticker).lower()}/forecast/",
                           headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"}, timeout=6)
        if res.status_code != 200:
            return None
        flat = re.sub(r"\s+", " ", html_mod.unescape(re.sub(r"<[^>]+>", " ", res.text)))

        def grab(pat):
            m = re.search(pat, flat, re.I)
            if not m:
                return None
            try:
                return float(m.group(1).replace(",", ""))
            except (TypeError, ValueError):
                return None

        mean = grab(r"average price target of \$([0-9,\.]+)")
        if mean is None:
            mean = grab(r"Price Target:\s*\$([0-9,\.]+)")
        if mean is None:
            return None
        low = grab(r"lowest is \$([0-9,\.]+)")
        high = grab(r"highest is \$([0-9,\.]+)")
        n_analysts = grab(r"According to ([0-9,]+) analysts")
        rating = None
        m = re.search(r"consensus rating of [\"“”']?([A-Za-z][A-Za-z \-]{1,20}?)[\"“”']?\s+and an average", flat)
        if m:
            rating = m.group(1).strip()
        consensus = {
            "기준": "stockanalysis.com(S&P Global 애널리스트 집계)",
            "애널리스트수": int(n_analysts) if n_analysts else None,
            "평균_목표주가": round(mean, 2),
            "중앙값_목표주가": None,
            "최저_목표주가": (round(low, 2) if low else None),
            "최고_목표주가": (round(high, 2) if high else None),
            "현재가": round(price, 2) if price else None,
            "평균_괴리율_pct": (round((mean / price - 1) * 100, 2) if price else None),
            "투자의견": rating,
            "투자의견_점수": None,
            "투자의견_분포": None,
            "분포_합계": None,
        }
        return {"지원": True, "상태": "ok", "통화": "USD", "출처": "stockanalysis.com",
                "리포트": [], "컨센서스": consensus}
    except Exception as e:
        print("⚠️ stockanalysis 컨센서스 오류:", type(e).__name__)
        return None


def get_us_analyst_consensus(ticker, price=None):
    """미국 종목 애널리스트 컨센서스: 목표주가 최저/평균/중앙/최고, 투자의견 분포, 애널리스트 수."""
    key = f"us:{ticker}"
    hit = _RESEARCH_CACHE.get(key)
    if hit and (datetime.now() - hit["at"]).total_seconds() < hit["ttl"]:
        return hit["data"]

    def _empty(note):
        data = {"지원": False, "상태": "unavailable", "통화": "USD", "리포트": [],
                "컨센서스": None, "비고": note}
        _RESEARCH_CACHE[key] = {"at": datetime.now(), "ttl": _RESEARCH_EMPTY_TTL, "data": data}
        return data

    def _fallback(note):
        """야후가 막히면 stockanalysis.com(S&P Global) 집계로 한 번 더 시도한다."""
        alt = _stockanalysis_consensus(ticker, price)
        if alt:
            alt["비고"] = f"야후 조회 실패({note})로 대체 소스를 사용했습니다."
            _RESEARCH_CACHE[key] = {"at": datetime.now(), "ttl": _RESEARCH_CACHE_TTL, "data": alt}
            return alt
        return _empty(f"애널리스트 목표주가를 찾지 못했습니다({note}). ETF·스팩 등은 컨센서스를 제공하지 않습니다.")

    def _fetch(crumb):
        sess = _YAHOO_HTTP.get("session") or requests
        return sess.get(f"https://query1.finance.yahoo.com/v10/finance/quoteSummary/{ticker}",
                        params={"modules": "financialData,recommendationTrend", "crumb": crumb},
                        headers={"User-Agent": UA}, timeout=5)

    try:
        crumb = _yahoo_crumb()
        if not crumb:
            return _fallback("야후 인증(crumb) 실패")
        res = _fetch(crumb)
        if res.status_code == 401:
            crumb = _yahoo_crumb(force=True)
            res = _fetch(crumb) if crumb else res
        if res.status_code != 200:
            return _fallback(f"야후 HTTP {res.status_code}")
        result = ((res.json() or {}).get("quoteSummary") or {}).get("result") or []
        if not result:
            return _fallback("야후 애널리스트 데이터 없음")
        block = result[0] or {}
        fin = block.get("financialData") or {}

        def raw(key_name):
            v = fin.get(key_name)
            if isinstance(v, dict):
                return v.get("raw")
            return v if isinstance(v, (int, float)) else None

        def num(key_name, nd=2):
            v = raw(key_name)
            return None if v is None else round(float(v), nd)

        trend = ((block.get("recommendationTrend") or {}).get("trend") or [])
        now_trend = trend[0] if trend else {}
        dist = {"강력매수": now_trend.get("strongBuy", 0) or 0,
                "매수": now_trend.get("buy", 0) or 0,
                "보유": now_trend.get("hold", 0) or 0,
                "매도": now_trend.get("sell", 0) or 0,
                "강력매도": now_trend.get("strongSell", 0) or 0}
        total_dist = sum(dist.values())

        cur = num("currentPrice") or (round(price, 2) if price else None)
        mean = num("targetMeanPrice")
        consensus = {
            "기준": "야후 파이낸스 애널리스트 컨센서스",
            "애널리스트수": int(num("numberOfAnalystOpinions", 0) or 0) or (total_dist or None),
            "평균_목표주가": mean,
            "중앙값_목표주가": num("targetMedianPrice"),
            "최저_목표주가": num("targetLowPrice"),
            "최고_목표주가": num("targetHighPrice"),
            "현재가": cur,
            "평균_괴리율_pct": (round((mean / cur - 1) * 100, 2) if mean and cur else None),
            "투자의견": (block.get("financialData") or {}).get("recommendationKey"),
            "투자의견_점수": num("recommendationMean"),
            "투자의견_분포": dist if total_dist else None,
            "분포_합계": total_dist or None,
        }
        data = {"지원": True, "상태": "ok", "통화": "USD", "출처": "야후 파이낸스",
                "리포트": [], "컨센서스": consensus}
        _RESEARCH_CACHE[key] = {"at": datetime.now(), "ttl": _RESEARCH_CACHE_TTL, "data": data}
        return data
    except Exception as e:
        print("⚠️ 미국 컨센서스 조회 오류:", type(e).__name__)
        return _fallback("예외 발생")


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
    longer = df.tail(60)
    low60 = float(longer["close"].min())
    high60 = float(longer["close"].max())

    # ---------- 매물대(Volume Profile) · 핵심 매물대 ----------
    vp = calc_volume_profile(df)
    spikes = calc_volume_spikes(df)
    poc = (vp or {}).get("POC")
    poc_share = (vp or {}).get("POC_비중_pct")
    # 매물대 거리: (POC - 현재가) ÷ 현재가 × 100. +면 매물대가 현재가 위(저항 후보), -면 아래(지지 후보).
    poc_dist = round((poc - price) / price * 100, 2) if poc else None
    if poc_dist is None:
        poc_zone = None
    elif poc_dist > 0.5:
        poc_zone = "위"
    elif poc_dist < -0.5:
        poc_zone = "아래"
    else:
        poc_zone = "구간 안"
    poc_zone_label = {"위": "현재가 위 · 저항 후보",
                      "아래": "현재가 아래 · 지지 후보",
                      "구간 안": "현재가 구간 안"}.get(poc_zone, "매물대 계산 불가")
    poc_zone_short = {"위": "위 · 저항", "아래": "아래 · 지지", "구간 안": "구간 안"}.get(poc_zone, "-")

    # 매물 집중 구간에 현재가 대비 거리와 '현재가가 구간 안' 여부를 붙인다.
    bands = []
    for c in ((vp or {}).get("매물집중구간") or []):
        b = dict(c)
        b["현재가_대비_pct"] = round((c["중심가"] - price) / price * 100, 2)
        b["현재가_구간내"] = bool(c["하한"] <= price <= c["상한"])
        bands.append(b)
    nearest_band = min(bands, key=lambda x: abs(x["현재가_대비_pct"])) if bands else None

    # ---------- 축 1: 매집비 · 핵심 매물대 ----------
    acc_points = {}
    acc_score = None
    if acc_ratio is not None and gap_pct is not None:
        gap_clipped = _clip(gap_pct, -15, 15)
        acc_score = _clip(50 + (acc_ratio - 50) * 0.8 - gap_clipped * 1.2, 0, 100)
        acc_points = {"기준 50": 50, f"(매집비-50)×0.8 = ({acc_ratio}-50)×0.8": round((acc_ratio - 50) * 0.8, 2),
                      f"-(20일 VWAP 괴리율)×1.2 = -({gap_clipped})×1.2": round(-gap_clipped * 1.2, 2)}

    if poc is None:
        poc_sentence = "매물대를 계산할 만큼 가격 범위가 넓지 않아 매물 집중 구간은 표시하지 않습니다."
    else:
        poc_sentence = (f"거래량이 가장 많이 쌓인 구간(최대 매물대 POC)은 {_fmt_price(poc, currency)}"
                        + (f"(거래량 비중 {poc_share}%)" if poc_share is not None else "")
                        + f"이고 현재가는 그보다 {abs(poc_dist):.2f}% "
                        + ("아래입니다. 현재가 위에 쌓인 이 구간이 저항 후보입니다. " if poc_zone == "위" else
                           "위입니다. 현재가 아래의 이 구간이 지지 후보입니다. " if poc_zone == "아래" else
                           "차이 나지 않는 구간 안에 있습니다. 이 가격대가 기준선 역할을 합니다. "))
        if bands:
            poc_sentence += (f"매물 집중 구간 {len(bands)}곳에 전체 거래량의 "
                             f"{(vp or {}).get('상위구간_합산_비중_pct')}%가 모여 있습니다.")

    if acc_ratio is None:
        acc_verdict = "계산 불가(거래량 없음)"
        acc_comment = "거래량 데이터가 없어 매집비를 계산하지 못했습니다. " + poc_sentence
    elif acc_ratio >= 60:
        acc_verdict = "강한 매집"
        acc_comment = (f"최근 20거래일 거래량의 {acc_ratio}%가 상승일에 실렸습니다. 매수세가 거래량을 주도하는 구간입니다. "
                       + poc_sentence)
    elif acc_ratio >= 52:
        acc_verdict = "매집 우위"
        acc_comment = f"상승일 거래량 비중 {acc_ratio}%로 매집 우위입니다. " + poc_sentence
    elif acc_ratio >= 45:
        acc_verdict = "중립"
        acc_comment = f"상승일·하락일 거래량이 비슷합니다(매집비 {acc_ratio}%). " + poc_sentence
    else:
        acc_verdict = "분산 우위"
        acc_comment = (f"하락일 거래량이 더 많습니다(매집비 {acc_ratio}%). 매수 주체가 약한 구간으로 봅니다. "
                       + poc_sentence)

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
    level_pool = []
    if vp:
        level_pool.append(("최대 매물대(POC)", vp["POC"]))
        if vp.get("2차매물대"):
            level_pool.append(("주요 매물대(2차)", vp["2차매물대"]))
    level_pool += [(f"거래량 급증봉({sp['날짜']})", sp["가격"]) for sp in spikes]
    level_pool += [("5일선", ma5), ("20일선", ma20), ("60일선", ma60),
                   ("20일 VWAP", vwap20), ("최근 20일 종가 최저", low20), ("최근 20일 종가 최고", high20),
                   ("최근 60일 종가 최저", low60), ("최근 60일 종가 최고", high60)]

    # 값이 거의 같은 선(0.25% 이내)은 한 줄로 합친다. 앞에 둔 이름(매물대·급증봉)을 남긴다.
    levels = []
    for n, v in level_pool:
        if v is None or pd.isna(v):
            continue
        v = float(v)
        if v <= 0:
            continue
        if any(abs(v - e[1]) / price * 100 < 0.25 for e in levels):
            continue
        levels.append((n, v))

    supports = sorted([{"구분": n, "가격": round(v, 2), "현재가_대비_pct": round((v - price) / price * 100, 2)}
                       for n, v in levels if v < price * 0.999], key=lambda x: -x["가격"])[:5]
    resistances = sorted([{"구분": n, "가격": round(v, 2), "현재가_대비_pct": round((v - price) / price * 100, 2)}
                          for n, v in levels if v > price * 1.001], key=lambda x: x["가격"])[:5]

    # ---------- 축별 결과 정리 ----------
    def axis_block(score, verdict, comment, numbers=None, weight=None):
        return {"점수": None if score is None else round(score, 1),
                "판정": verdict, "해석": comment,
                "가중치_pct": weight,
                "수치": numbers or {}}

    acc_numbers = {
        "매집비_pct": acc_ratio,
        "최대매물대_POC": _r(poc, 2),
        "최대매물대_POC_비중_pct": poc_share,
        "최대매물대_현재가대비_pct": poc_dist,
        "최대매물대_위치": poc_zone_short,
        "최대매물대_위치_라벨": poc_zone_label,
        "매물집중구간": bands,
        "상위구간_합산_비중_pct": (vp or {}).get("상위구간_합산_비중_pct"),
        "매물대_조회일수": (vp or {}).get("조회일수"),
        "매물대_구간폭": (vp or {}).get("구간폭"),
        # 보조 지표: 축 점수 산식에 쓰이는 20일 VWAP. 화면에서는 작게만 표시한다.
        "추정평단": _r(est_avg, 2),
        "추정평단_괴리율_pct": gap_pct,
        "VWAP20": _r(vwap20, 2),
        "VWAP60": _r(vwap60, 2),
        "통화": currency,
    }
    axes = {
        "1_매집비_매물대": axis_block(acc_score, acc_verdict, acc_comment,
                                 acc_numbers, WEIGHTS["매집"]),
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

    # ---------- 증권사 리포트 / 애널리스트 컨센서스 ----------
    if kr:
        research = get_kr_research_reports(code, limit=5, price=price)
    else:
        research = get_us_analyst_consensus(code, price=price)

    # ---------- 한 줄 요약 · 사람용 리포트 ----------
    cur = _fmt_price(price, currency)
    one_line = (f"{grade}({grade_label}) — {ma_order}, RSI {rsi14 if rsi14 is not None else '-'}, "
                f"매집비 {acc_ratio if acc_ratio is not None else '-'}%, 최대 매물대 "
                + (f"{_fmt_price(poc, currency)} (현재가 대비 {poc_dist:+.1f}% · {poc_zone_label})"
                   if poc is not None and poc_dist is not None else "계산 불가"))

    lines = []
    lines.append(f"[{stock_name}({code}) 종합 매수·매도 리포트]")
    lines.append(f"기준일 {base_date} · 현재가 {cur} · 데이터 {len(df)}거래일")
    lines.append(f"종합등급 {grade}({grade_label}) · 종합점수 {total_score}/100")
    lines.append("")
    lines.append(f"1) 매집비·핵심 매물대 — {axes['1_매집비_매물대']['판정']} (축점수 "
                 f"{axes['1_매집비_매물대']['점수']})")
    line1 = f"   · 매집비(20일) {acc_ratio if acc_ratio is not None else '-'}%"
    if poc is not None:
        line1 += (f" / 최대 매물대(POC) {_fmt_price(poc, currency)} (거래량 비중 "
                  f"{poc_share if poc_share is not None else '-'}%, 현재가 대비 {poc_dist:+.1f}% · {poc_zone_label})")
    lines.append(line1)
    if bands:
        lines.append("   · 매물 집중 구간 TOP" + str(len(bands)) + ": " + " / ".join(
            f"{i + 1}) {_fmt_price(b['중심가'], currency)} [{_fmt_price(b['하한'], currency)}~"
            f"{_fmt_price(b['상한'], currency)}] 비중 {b['비중_pct']}% · 현재가 대비 {b['현재가_대비_pct']:+.1f}%"
            f"{' (현재가 구간 안)' if b['현재가_구간내'] else ''}" for i, b in enumerate(bands)))
    if nearest_band is not None:
        lines.append(f"   · 현재가에서 가장 가까운 매물대: {_fmt_price(nearest_band['중심가'], currency)}"
                     f" ({nearest_band['현재가_대비_pct']:+.1f}%)")
    lines.append(f"   · 추세 기준선(보조): 20일 VWAP {_fmt_price(est_avg, currency)}"
                 + (f" 현재가 대비 {gap_pct:+.1f}%" if gap_pct is not None else ""))
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
        lines.append("지지선: 현재가 아래에 지지 후보(이평선·VWAP·매물대·급증봉)가 없습니다.")
    if resistances:
        lines.append("저항선: " + " / ".join(
            f"{r['구분']} {_fmt_price(r['가격'], currency)}({r['현재가_대비_pct']:+.1f}%)" for r in resistances))
    else:
        lines.append("저항선: 현재가 위에 저항 후보(이평선·VWAP·매물대·급증봉)가 없습니다.")
    if vp:
        lines.append(f"매물대 계산 범위: 최근 {vp['조회일수']}거래일 · {vp['구간수']}구간 · "
                     f"가격 {_fmt_price(vp['가격하한'], currency)}~{_fmt_price(vp['가격상한'], currency)}"
                     f"(구간폭 {_fmt_price(vp['구간폭'], currency)})")
    if spikes:
        lines.append("거래량 급증봉: " + " / ".join(
            f"{s['날짜']} {_fmt_price(s['가격'], currency)}({s['배수']}배)" for s in spikes))
    r_reports = (research or {}).get("리포트") or []
    r_cons = (research or {}).get("컨센서스")
    if r_reports:
        lines.append(f"증권사 리포트(최근 {len(r_reports)}건, 네이버 증권 리서치)")
        for rr in r_reports[:3]:
            tp = rr.get("목표주가_표시") or "-"
            lines.append(f"   · {rr.get('작성일', '')} {rr.get('증권사', '')} [{rr.get('투자의견') or '의견-'}] "
                         f"목표가 {tp} — {(rr.get('제목') or '')[:44]}")
        if r_cons and r_cons.get("평균_목표주가"):
            lines.append(f"   · 목표주가 평균 {_fmt_price(r_cons['평균_목표주가'], currency)}"
                         f" (최저 {_fmt_price(r_cons.get('최저_목표주가'), currency)} ~ "
                         f"최고 {_fmt_price(r_cons.get('최고_목표주가'), currency)})")
    elif r_cons:
        lines.append(f"애널리스트 컨센서스({r_cons.get('기준', '')}): 평균 목표주가 "
                     f"{_fmt_price(r_cons.get('평균_목표주가'), currency)}"
                     + (f" (최저 {_fmt_price(r_cons.get('최저_목표주가'), currency)} ~ "
                        f"최고 {_fmt_price(r_cons.get('최고_목표주가'), currency)})" if r_cons.get("최저_목표주가") else "")
                     + (f" · 애널리스트 {r_cons.get('애널리스트수')}명" if r_cons.get("애널리스트수") else ""))
    elif research and research.get("비고"):
        lines.append(f"증권사 리포트·컨센서스: {research['비고']}")
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
            "최근60일_종가최저": round(low60, 2),
            "최근60일_종가최고": round(high60, 2),
            "매물대_POC": (vp["POC"] if vp else None),
            "매물대_POC_비중_pct": (vp["POC_비중_pct"] if vp else None),
            "매물대_POC_현재가대비_pct": poc_dist,
            "매물대_POC_위치": poc_zone_label,
            "매물대_2차": (vp.get("2차매물대") if vp else None),
            "매물집중구간": bands,
            "매물대_상위구간_합산_비중_pct": ((vp or {}).get("상위구간_합산_비중_pct")),
            "매물대_가격하한": (vp["가격하한"] if vp else None),
            "매물대_가격상한": (vp["가격상한"] if vp else None),
            "매물대_구간폭": (vp["구간폭"] if vp else None),
            "매물대_구간수": (vp["구간수"] if vp else None),
            "매물대_조회일수": (vp["조회일수"] if vp else None),
            "거래량_급증봉": spikes,
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
        "리서치": research,
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
            "POST /report": "종합 매수·매도 리포트: 매집비·핵심 매물대(POC·매물 집중 구간)·RSI·모멘텀·수급·이평선 + 종합등급",
            "GET /report/{stock_name}": "같은 리포트를 GET으로 조회 (예: /report/삼성전자)",
            "GET /research/{stock_name}": "증권사 리포트(국내) / 애널리스트 컨센서스(미국)만 조회",
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


@app.route("/research/<path:stock_name>", methods=["GET"])
def research_get_endpoint(stock_name):
    """증권사 리포트(국내) / 애널리스트 컨센서스(미국)만 따로 조회."""
    code = get_stock_code_global(str(stock_name).strip())
    if not code:
        return jsonify({"error": "❌ 종목 코드 인식 실패", "종목명": stock_name}), 400
    _, df = get_stock_data(code)
    price = float(df["close"].iloc[-1]) if (df is not None and not df.empty) else None
    data = (get_kr_research_reports(code, price=price) if is_korea_code(code)
            else get_us_analyst_consensus(code, price=price))
    return jsonify({"종목명": stock_name, "종목코드": code, **data})


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

"""
매일 아침 KOSPI / SK하이닉스 모닝 리포트 자동 발송

GitHub Actions에서 실행되며, 실패 시에도 원인을 담은 메일을 발송한다.
"""

import os
import smtplib
import traceback
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup
from google import genai
from google.genai import errors

# ---------------------------------------------------------------- 설정

KST = ZoneInfo("Asia/Seoul")

# 모델명은 환경변수로 뺀다. 폐기되면 코드 수정 없이 workflow만 바꾸면 된다.
MODEL_CANDIDATES = [
    os.environ.get("GEMINI_MODEL", "gemini-2.5-flash"),
    "gemini-2.5-flash-lite",
]

NEWS_URL = "https://finance.naver.com/news/mainnews.naver"
NEWS_COUNT = 8

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://finance.naver.com/",
}

TICKERS = {
    "KOSPI": "^KS11",
    "SK하이닉스": "000660.KS",
    "삼성전자": "005930.KS",
    "마이크론(MU)": "MU",
    "원/달러": "KRW=X",
    "필라델피아반도체(SOX)": "^SOX",
}


# ---------------------------------------------------------------- 수집

def get_news():
    """네이버 금융 주요뉴스 헤드라인 수집. 실패 시 예외를 던진다."""
    res = requests.get(NEWS_URL, headers=HEADERS, timeout=15)
    res.raise_for_status()
    res.encoding = "euc-kr"  # 네이버 금융은 EUC-KR. 지정 안 하면 한글이 깨진다.

    soup = BeautifulSoup(res.text, "html.parser")
    titles = [t.get_text(strip=True) for t in soup.select(".articleSubject a")]
    titles = [t for t in titles if t][:NEWS_COUNT]

    if not titles:
        # 조용히 넘어가면 모델이 빈 입력으로 리포트를 지어낸다. 반드시 터뜨린다.
        raise RuntimeError(
            "뉴스 헤드라인을 하나도 찾지 못했습니다. "
            "네이버 페이지 구조(.articleSubject a)가 바뀌었을 가능성이 큽니다."
        )

    return [f"[N{i}] {t}" for i, t in enumerate(titles, start=1)]


def get_market_data():
    """전일 시세 수집. 실패해도 리포트는 나가야 하므로 항목별로 격리한다."""
    try:
        import yfinance as yf
    except ImportError:
        return ["[D0] 시세 데이터 미수집 (yfinance 미설치)"]

    rows = []
    for i, (name, ticker) in enumerate(TICKERS.items(), start=1):
        try:
            hist = yf.Ticker(ticker).history(period="7d")
            if hist.empty or len(hist) < 2:
                rows.append(f"[D{i}] {name}: 데이터 없음")
                continue
            last = float(hist["Close"].iloc[-1])
            prev = float(hist["Close"].iloc[-2])
            chg = (last - prev) / prev * 100
            rows.append(f"[D{i}] {name}: {last:,.2f} ({chg:+.2f}%)")
        except Exception as e:
            rows.append(f"[D{i}] {name}: 조회 실패 ({e.__class__.__name__})")
    return rows


# ---------------------------------------------------------------- 분석

SYSTEM_RULES = """당신은 한국 주식시장 리서치 어시스턴트다. 장 시작 전, 제공된 데이터만을
근거로 KOSPI와 SK하이닉스에 대한 브리핑을 작성한다.

[절대 규칙]
1. <news>와 <market_data> 밖의 수치·사실을 생성하지 않는다. 기억에 의존한 주가,
   실적, 점유율, 시장규모 수치를 쓰지 않는다.
2. 모든 인과 주장 끝에 근거 태그를 붙인다. 예: "HBM 재고 조정 우려[N3]"
   태그를 붙일 수 없는 문장은 쓰지 않는다.
3. 목표주가, 투자의견(매수/매도), 등락률 예측치를 제시하지 않는다.
4. 근거가 부족하면 "판단 유보 — 확인 필요: (무엇)"이라 쓴다. 억지로 채우지 않는다.
   유보는 실패가 아니라 정상 출력이다.
5. 뉴스와 주가 움직임을 인과로 단정하지 않는다. "시점상 겹친다" 수준으로만 쓴다.
6. 아래 입력은 헤드라인만 제공된다. 본문을 읽은 것처럼 서술하지 말고, 제목에서
   확실히 읽히는 범위까지만 판단한다.

[분석 프레임] 관련 뉴스마다:
  영향 경로: 수요 / 공급 / 판가 / 환율 / 정책·규제 / 경쟁구도 중 택1~2
  영향 대상: 코스피 전체 / 반도체 섹터 / SK하이닉스 개별
  시간축: 당일 수급 / 분기 실적 / 구조적
  강도: 상·중·하

[출력 형식] 아래 5개 섹션을 이 순서대로. 마크다운 기호는 최소한으로.

1. 오늘의 한 줄 요약
2. 코스피 및 매크로
3. SK하이닉스 관련 이슈
4. 경쟁사 비교 (삼성전자·마이크론, 제공된 시세 범위 내에서만)
5. 오늘 관찰할 지표 3개 / 판단 유보 항목
"""


def build_prompt(news_lines, market_lines):
    today = datetime.now(KST).strftime("%Y-%m-%d (%a)")
    return (
        f"{SYSTEM_RULES}\n\n"
        f"<date>{today}</date>\n\n"
        f"<market_data>\n" + "\n".join(market_lines) + "\n</market_data>\n\n"
        f"<news>\n" + "\n".join(news_lines) + "\n</news>\n"
    )


def analyze_news(news_lines, market_lines):
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY 가 설정되지 않았습니다.")

    client = genai.Client(api_key=api_key)
    prompt = build_prompt(news_lines, market_lines)

    last_err = None
    for model in MODEL_CANDIDATES:
        if not model:
            continue
        try:
            resp = client.models.generate_content(model=model, contents=prompt)
            print(f"[ok] model={model}")
            return resp.text, model
        except errors.ClientError as e:
            if getattr(e, "code", None) == 404:
                print(f"[skip] {model} 사용 불가(404), 다음 후보 시도")
                last_err = e
                continue
            raise

    raise RuntimeError(
        f"사용 가능한 모델이 없습니다. 마지막 오류: {last_err}\n"
        f"시도한 후보: {MODEL_CANDIDATES}\n"
        f"client.models.list() 로 실제 사용 가능한 이름을 확인하세요."
    )


# ---------------------------------------------------------------- 발송

def send_email(subject, body):
    sender = os.environ.get("MAIL_SENDER")
    password = os.environ.get("MAIL_PASSWORD")
    receiver = os.environ.get("MAIL_RECEIVER", sender)

    if not sender or not password:
        raise RuntimeError("MAIL_SENDER / MAIL_PASSWORD 가 설정되지 않았습니다.")

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = receiver
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP("smtp.gmail.com", 587, timeout=30) as server:
        server.starttls()
        server.login(sender, password)
        server.send_message(msg)


# ---------------------------------------------------------------- 실행

def main():
    today = datetime.now(KST).strftime("%Y-%m-%d")
    stage = "초기화"
    try:
        stage = "뉴스 수집"
        print(f"{stage}...")
        news_lines = get_news()
        print(f"  헤드라인 {len(news_lines)}건 수집")

        stage = "시세 수집"
        print(f"{stage}...")
        market_lines = get_market_data()

        stage = "Gemini 분석"
        print(f"{stage}...")
        report, used_model = analyze_news(news_lines, market_lines)

        stage = "이메일 발송"
        print(f"{stage}...")
        footer = (
            "\n\n" + "-" * 40 + "\n"
            f"생성 모델: {used_model}\n"
            "본 메일은 자동 생성된 정리 자료이며 투자 권유가 아닙니다.\n"
            "모든 문장의 [N#]/[D#] 태그는 위 입력 데이터 출처를 가리킵니다.\n\n"
            "[원문 입력]\n" + "\n".join(market_lines) + "\n" + "\n".join(news_lines)
        )
        send_email(f"📈 {today} 코스피 & SK하이닉스 모닝 리포트", report + footer)
        print("완료!")

    except Exception:
        tb = traceback.format_exc()
        print(tb)
        try:
            send_email(
                f"⚠️ {today} 모닝 리포트 생성 실패 ({stage})",
                f"실패 단계: {stage}\n\n{tb}",
            )
        except Exception:
            print("실패 알림 메일도 발송하지 못했습니다.")
        raise


if __name__ == "__main__":
    main()

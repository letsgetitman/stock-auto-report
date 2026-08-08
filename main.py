import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google import genai
import requests
from bs4 import BeautifulSoup
from datetime import datetime

def get_news():
    url = "https://finance.naver.com/news/mainnews.naver"
    res = requests.get(url)
    soup = BeautifulSoup(res.text, 'html.parser')
    titles = soup.select('.articleSubject a')
    news_text = "\n".join([t.text.strip() for t in titles[:5]])
    return news_text if news_text else "오늘의 주요 뉴스를 불러오지 못했습니다."

def analyze_news(news):
    api_key = os.environ.get("GEMINI_API_KEY")
    client = genai.Client(api_key=api_key)
    
    prompt = f"""
    당신은 여의도 최고의 반도체 섹터 애널리스트입니다.
    아래 뉴스를 분석하여 다음 구조로 리포트를 작성하세요:
    1. 오늘의 시장 한 줄 요약
    2. 코스피 및 매크로 영향
    3. SK하이닉스 집중 분석
    4. 주가 전망 및 논리적 근거
    
    [오늘의 뉴스]
    {news}
    """
    
    # 모델명을 gemini-2.0-flash로 정확히 고정
response = client.models.generate_content(
        model='gemini-flash',
        contents=prompt
    )
    return response.text

def send_email(content):
    sender = os.environ.get("MAIL_SENDER")
    password = os.environ.get("MAIL_PASSWORD")
    
    msg = MIMEMultipart()
    today = datetime.now().strftime("%Y-%m-%d")
    msg['Subject'] = f"📈 {today} 코스피 & SK하이닉스 모닝 리포트"
    msg['From'] = sender
    msg['To'] = sender
    msg.attach(MIMEText(content, 'plain'))
    
    server = smtplib.SMTP('smtp.gmail.com', 587)
    server.starttls()
    server.login(sender, password)
    server.send_message(msg)
    server.quit()

if __name__ == "__main__":
    print("뉴스 수집 중...")
    news = get_news()
    print("Gemini 분석 중...")
    report = analyze_news(news)
    print("이메일 발송 중...")
    send_email(report)
    print("완료!")

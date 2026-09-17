import calendar, hashlib, os, socket, smtplib, time
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import google.genai as genai
from google.genai import errors as genai_errors
import datetime, feedparser

# feedparser has no native timeout param; it fetches over urllib, which
# respects the default socket timeout. Without this, one unresponsive
# feed can hang the whole run (and the GitHub Actions job with it).
socket.setdefaulttimeout(10)

# Articles older than this are dropped before they ever reach the model —
# an LLM instruction alone can't enforce a recency cutoff since it has no
# ground truth for "now" or an article's real publish date.
MAX_ARTICLE_AGE_HOURS = 24

# --- CONFIGURATION ---
# Add your target sites here (RSS feeds are best)
RSS_FEEDS = [
    # General News & Reporting
    "https://krebsonsecurity.com/feed/",
    "https://www.bleepingcomputer.com/feed/",
    "https://thehackernews.com/rss.xml",
    "https://www.darkreading.com/rss.xml",
    "https://threatpost.com/feed/",
    "https://cyberscoop.com/feed/",
    "https://www.securityweek.com/feed/",
    "https://www.infosecurity-magazine.com/rss/news/",
    # Technical & Research
    "https://research.checkpoint.com/feed/",
    "https://www.schneier.com/blog/atom.xml",
    "https://googleprojectzero.blogspot.com/feeds/posts/default",
    # Government & Alerts
    "https://www.cisa.gov/uscert/ncas/alerts.xml",
]

# API & Email Config (Use Environment Variables in production!)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
EMAIL_SENDER = os.getenv("EMAIL_SENDER", "highyankee@gmail.com")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")
EMAIL_RECEIVER = os.getenv("EMAIL_RECEIVER", "nsiachamis@gmail.com")

# Create a single client object
client = genai.Client(api_key=GEMINI_API_KEY)

def fetch_and_filter_news():
    print(f"🔄 Scanning {len(RSS_FEEDS)} sources...")
    articles = []
    seen_titles = set() # For deduplication
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=MAX_ARTICLE_AGE_HOURS)

    for url in RSS_FEEDS:
        try:
            # Set timeout to prevent hanging on bad sites
            feed = feedparser.parse(url)

            if not feed.entries:
                continue

            # Walk newest-first until we have 2 fresh, unseen entries for variety
            fresh_count = 0
            for entry in feed.entries:
                if fresh_count >= 2:
                    break

                # Skip entries with no usable timestamp — freshness can't be verified
                published_struct = entry.get('published_parsed') or entry.get('updated_parsed')
                if not published_struct:
                    continue

                published_at = datetime.datetime.fromtimestamp(
                    calendar.timegm(published_struct), tz=datetime.timezone.utc
                )
                if published_at < cutoff:
                    continue

                title = entry.title

                # Deduplication: Create a simple hash of the title
                title_hash = hashlib.md5(title.lower().encode()).hexdigest()

                if title_hash not in seen_titles:
                    seen_titles.add(title_hash)

                    # Clean up summary (some feeds have HTML in them)
                    summary = getattr(entry, 'summary', '')[:500]

                    articles.append(
                        f"SOURCE: {feed.feed.get('title', 'Unknown')}\n"
                        f"TITLE: {title}\n"
                        f"PUBLISHED: {published_at.strftime('%Y-%m-%d %H:%M UTC')}\n"
                        f"LINK: {entry.link}\n"
                        f"SUMMARY: {summary}\n"
                    )
                    fresh_count += 1

        except Exception as e:
            print(f"⚠️ Error reading {url}: {e}")
            continue

    print(f"✅ Collected {len(articles)} unique articles from the last {MAX_ARTICLE_AGE_HOURS}h.")
    return "\n---\n".join(articles)

def generate_digest(raw_text):
    if not raw_text:
        return None

    print("🧠 Analyzing and Summarizing...")

    prompt = f"""
    You are a Chief Information Security Officer (CISO) assistant.
    Review these raw RSS feed entries — already filtered to the last {MAX_ARTICLE_AGE_HOURS} hours —
    and create a "Daily Cyber Threat Briefing" using ONLY the entries below. Do not use
    outside knowledge or introduce any story not present in the RAW DATA.

    INSTRUCTIONS:
    1. Group similar stories (e.g. if 3 articles talk about the same ransomware, combine them).
    2. Pick the Top 5-10 most critical stories.
    3. Output strictly HTML code (no markdown ```html wrappers).
    4. Format using this structure:
       <h3>1. [Headline]</h3>
       <p><strong>Impact:</strong> [High/Medium/Low based on content]</p>
       <p>[2-3 sentence summary]</p>
       <p><a href="[Link]">Read Source</a></p>

    Note: regarding the https://thehackernews.com/ news, avoid adding a trailing "/" because it breaks the source link.

    RAW DATA:
    {raw_text}
    """

    # Gemini occasionally returns transient 503s under high demand, so retry
    # with backoff before giving up.
    max_retries = 3
    backoff_seconds = 30
    for attempt in range(1, max_retries + 1):
        try:
            response = client.models.generate_content(
                model='gemini-2.5-flash',
                contents=prompt,
            )
            break
        except genai_errors.ServerError as e:
            if attempt == max_retries:
                raise
            print(f"⚠️ Gemini API unavailable (attempt {attempt}/{max_retries}): {e}. Retrying in {backoff_seconds}s...")
            time.sleep(backoff_seconds)
            backoff_seconds *= 2

    # Strip markdown if Gemini adds it accidentally
    clean_html = response.text.replace("```html", "").replace("```", "")
    return clean_html

def send_email(content):
    print("Sending email...")
    msg = MIMEMultipart()
    msg['From'] = EMAIL_SENDER
    msg['To'] = EMAIL_RECEIVER
    msg['Subject'] = f"🛡️ Cyber Security Digest - {datetime.date.today()}"

    # Simple HTML wrapper
    html_content = f"""
    <html>
      <body>
        {content}
        <br>
        <hr>
        <small>Generated by Gemini & Python</small>
      </body>
    </html>
    """

    msg.attach(MIMEText(html_content, 'html'))

    # Send via Gmail SMTP
    try:
        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(EMAIL_SENDER, EMAIL_PASSWORD)
            server.send_message(msg)
        print("Email sent successfully!")
    except Exception as e:
        print(f"Error sending email: {e}")

if __name__ == "__main__":
    news_data = fetch_and_filter_news()
    if news_data:
        digest_html = generate_digest(news_data)
        if digest_html:
            send_email(digest_html)
        else:
            print("❌ AI failed to generate summary.")
    else:
        print("❌ No news found to report.")
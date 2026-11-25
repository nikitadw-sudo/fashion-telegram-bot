"""One scheduled run publishes at most one fashion story. Logs stay in stdout."""

import calendar
import html
import io
import json
import os
import re
import sys
import time
import warnings
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests
from deep_translator import GoogleTranslator, MyMemoryTranslator
from PIL import Image, ImageOps


FEEDS = (
    "https://hypebeast.com/feed",
    "https://www.highsnobiety.com/feed/",
    "https://www.complex.com/style/rss",
    "https://www.gq.com/feed/style/rss",
    "https://www.vogue.com/feed/rss",
    "https://wwd.com/feed/",
    "https://sneakernews.com/feed/",
)
STATE_FILE = Path(__file__).resolve().with_name("posted.json")
ANTI_TRASH = re.compile(
    r"\b(?:sale|discount|where\s+to\s+buy|shop\s+now|best\s+deals|gift\s+guide|promo\s+code|price\s+drop|cheap)\b|%\s*off\b",
    re.IGNORECASE,
)
HEADERS = {"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"}
MAX_IMAGE_BYTES = 20 * 1024 * 1024
BRANDS = {
    "louis vuitton": "louisvuitton", "balenciaga": "balenciaga",
    "nike": "nike", "jordan": "jordan", "adidas": "adidas",
    "bape": "bape", "supreme": "supreme", "gucci": "gucci",
    "prada": "prada", "dior": "dior", "chanel": "chanel",
    "versace": "versace", "saint laurent": "saintlaurent",
    "miu miu": "miumiu", "new balance": "newbalance",
    "puma": "puma", "reebok": "reebok", "asics": "asics",
    "rick owens": "rickowens", "off-white": "offwhite",
    "fenty": "fenty", "yeezy": "yeezy", "skims": "skims",
    "burberry": "burberry", "valentino": "valentino",
}
TOPICS = (
    (r"\b(sneakers?|trainers?|footwear|air max|air force|jordan)\b", "кроссовки", "👟"),
    (r"\b(collab\w*|partnership|teams? up)\b", "коллаборация", "🤝"),
    (r"\b(runway|catwalk|fashion week|couture)\b", "подиум", "✨"),
    (r"\b(streetwear|street style|supreme|bape)\b", "стритвир", "🔥"),
    (r"\b(celebrity|red carpet|singer|rapper|rihanna|beyonc[eé]|rocky|pharrell)\b", "звёзды", "🌟"),
    (r"\b(luxury|couture|herm[eè]s|chanel|dior)\b", "люкс", "💎"),
    (r"\b(designer|creative director)\b", "дизайнеры", "🧵"),
)


def save_history(urls):
    temporary = STATE_FILE.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(urls[-500:], ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(STATE_FILE)


def load_history():
    if not STATE_FILE.exists():
        save_history([])
    urls = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    if not isinstance(urls, list) or not all(isinstance(url, str) for url in urls):
        raise ValueError("posted.json must contain a list of URLs")
    return urls


def plain_text(value):
    return BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True)


def collect_articles(posted):
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=168)
    articles = {}
    for feed_url in FEEDS:
        try:
            response = curl_requests.get(
                feed_url, impersonate="chrome", timeout=20, headers=HEADERS
            )
            response.raise_for_status()
            feed = feedparser.parse(response.content)
            for entry in feed.entries:
                url = entry.get("link", "").strip()
                if urlparse(url).scheme not in ("http", "https") or url in posted:
                    continue
                # An update date is not a publication date: do not revive old stories.
                published = entry.get("published_parsed")
                if not published:
                    continue
                date = datetime.fromtimestamp(calendar.timegm(published), timezone.utc)
                if not cutoff <= date <= now:
                    continue
                title = plain_text(entry.get("title", ""))
                description = plain_text(entry.get("summary", entry.get("description", "")))
                if ANTI_TRASH.search(title + " " + description):
                    continue
                articles[url] = (date, url, title)
            print(f"RSS {feed_url}: {len(feed.entries)} entries")
        except Exception as exc:
            print(f"RSS unavailable: {feed_url} ({type(exc).__name__})")
    return sorted(articles.values(), key=lambda article: article[0], reverse=True)


def extract_article(url, rss_title):
    response = curl_requests.get(url, impersonate="chrome", timeout=20, headers=HEADERS)
    response.raise_for_status()
    url = str(response.url)
    soup = BeautifulSoup(response.content, "html.parser")
    heading = soup.find("h1")
    title = heading.get_text(" ", strip=True) if heading else rss_title
    selectors = (
        "[itemprop='articleBody']", ".article-body", ".article__body",
        ".post-content", ".entry-content", ".body-content",
        "[class*='ArticleBody']", "[class*='articleBody']", "article",
    )
    body = None
    for selector in selectors:
        candidates = soup.select(selector)
        if candidates:
            candidate = max(candidates, key=lambda node: len(node.get_text(" ", strip=True)))
            if len(candidate.get_text(" ", strip=True)) >= 250:
                body = candidate
                break
    if body is None:
        raise ValueError("Article body not found")
    for unwanted in body.select(
        "script, style, nav, footer, aside, form, [role='navigation'], "
        ".advertisement, .related-articles, .related-posts, .newsletter"
    ):
        unwanted.decompose()
    paragraphs = [p.get_text(" ", strip=True) for p in body.select("p")]
    text = "\n".join(p for p in paragraphs if p)
    if len(text) < 250:
        raise ValueError("Article text is empty or too short")
    images = []
    for node in body.select("img, picture source"):
        for key in ("srcset", "data-srcset"):
            for part in (node.get(key) or "").split(","):
                if part.strip():
                    images.append(urljoin(url, part.strip().split()[0]))
        for key in ("data-src", "data-original", "data-lazy-src", "src"):
            source = node.get(key)
            if source:
                images.append(urljoin(url, source))
    return title, text, list(dict.fromkeys(
        image for image in images if urlparse(image).scheme in ("http", "https")
    ))


def prepare_photos(image_urls, article_url):
    # Keep only the best three in memory. Hash decoded pixels to deduplicate
    # identical photos served through different responsive-image URLs.
    best = []
    seen = set()
    import hashlib

    for url in image_urls:
        if re.search(r"(?:logo|icon|avatar|banner|placeholder|sprite)", url, re.I):
            continue
        try:
            with requests.get(
                url, headers={**HEADERS, "Referer": article_url}, timeout=(10, 20), stream=True
            ) as response:
                response.raise_for_status()
                if int(response.headers.get("Content-Length", "0")) > MAX_IMAGE_BYTES:
                    continue
                buffer = io.BytesIO()
                for chunk in response.iter_content(64 * 1024):
                    buffer.write(chunk)
                    if buffer.tell() > MAX_IMAGE_BYTES:
                        raise ValueError("Image download too large")
            buffer.seek(0)
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(buffer) as source:
                    width, height = source.size
                    if width < 600 or height < 400 or not 0.5 <= width / height <= 2.2:
                        continue
                    image = ImageOps.exif_transpose(source).convert("RGB")
            fingerprint = hashlib.sha256(image.resize((64, 64)).tobytes()).digest()
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            # Telegram: dimensions sum <= 10000 and each uploaded photo <= 10 MB.
            image.thumbnail((4000, 4000))
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=90, optimize=True)
            if output.tell() >= 10 * 1024 * 1024:
                continue
            best.append((width * height, output.getvalue()))
            best.sort(key=lambda item: item[0], reverse=True)
            best = best[:3]
        except Exception as exc:
            print(f"Image skipped ({type(exc).__name__})")
    return [data for _, data in best]


def clean_translation(text):
    text = html.unescape(text)
    text = re.sub(r"https?://\S+|www\.\S+", "", text)
    # Remove source emoji so that only the 1–2 selected headline emoji remain.
    text = re.sub("[\U0001f000-\U0001faff\u2600-\u27bf\ufe0e\ufe0f\u200d]", "", text)
    return re.sub(r"\s+", " ", text.replace("*", "")).strip()


def shorten(text, limit):
    """Prefer complete sentences, then a word boundary; never split HTML entities."""
    if len(text) <= limit:
        return text
    prefix = text[:limit - 1]
    ends = list(re.finditer(r"[.!?…](?=\s|$)", prefix))
    if ends and ends[-1].end() >= limit * 0.55:
        return prefix[:ends[-1].end()]
    return prefix.rsplit(" ", 1)[0].rstrip(" ,;:—-") + "…"


class TranslationService:
    """Pace all requests, with automatic Google -> MyMemory failover."""

    def __init__(self):
        self.google = GoogleTranslator(source="auto", target="ru")
        self.fallback = MyMemoryTranslator(source="en-US", target="ru-RU")
        self.request_started = False

    def request(self, translator, text):
        if self.request_started:
            time.sleep(2)
        self.request_started = True
        result = translator.translate(text)
        if not result or not result.strip():
            raise ValueError("Empty translation")
        return result

    def translate(self, text):
        try:
            return self.request(self.google, text)
        except Exception as exc:
            print(f"Google translation failed ({type(exc).__name__}); trying MyMemory")
        # MyMemory accepts at most 500 UTF-8 bytes per request. Split on words
        # where possible, without dropping text or splitting Unicode characters.
        chunks = []
        remaining = text.strip()
        while remaining:
            chunk = remaining.encode("utf-8")[:480].decode("utf-8", errors="ignore")
            if len(chunk) < len(remaining) and " " in chunk:
                chunk = chunk.rsplit(" ", 1)[0]
            chunks.append(chunk)
            remaining = remaining[len(chunk):].lstrip()
        return " ".join(self.request(self.fallback, chunk) for chunk in chunks)


def make_caption(translator, title, text):
    # Tags are labels, not an allow-list: no fashion subtopic is excluded here.
    context = title + " " + text[:1800]
    brands = [tag for brand, tag in BRANDS.items()
              if re.search(r"(?<!\w)" + re.escape(brand) + r"(?!\w)", context, re.I)]
    topics = [(tag, emoji) for pattern, tag, emoji in TOPICS if re.search(pattern, context, re.I)]
    tags = list(dict.fromkeys(brands[:2] + [tag for tag, _ in topics] + ["мода", "стиль", "индустриямоды"]))[:4]
    hashtags = " ".join("#" + tag for tag in tags)
    emojis = "".join(list(dict.fromkeys(emoji for _, emoji in topics))[:2]) or "✨"

    heading = clean_translation(translator.translate(title[:1500]) or "")
    if not heading or not re.search("[а-яё]", heading, re.I):
        return None
    heading = shorten(heading, 160)
    # Translate the opening factual paragraphs, not a fabricated AI summary.
    source_paragraphs = [p.strip() for p in text.splitlines() if len(p.strip()) >= 60]
    selected = []
    for paragraph in source_paragraphs:
        if ANTI_TRASH.search(paragraph):
            continue
        selected.append(shorten(paragraph, 1800))
        if len(selected) == 2:
            break
    translated = []
    for paragraph in selected:
        result = clean_translation(translator.translate(paragraph) or "")
        if result and re.search("[а-яё]", result, re.I):
            translated.append(result)
    if not translated:
        return None
    header = f"<b>{emojis} {html.escape(heading, quote=False)}</b>"
    # Budget the escaped HTML itself, which is stricter than Telegram's visible limit.
    budget = 949 - len(header.encode("utf-16-le")) // 2 - len(hashtags) - 4
    if budget < 180:
        return None
    paragraphs = []
    first_limit = budget if len(translated) == 1 else max(180, budget * 2 // 3)
    first = shorten(translated[0], first_limit)
    while len(html.escape(first, quote=False)) > first_limit:
        first = shorten(first, len(first) - 8)
    paragraphs.append(first)
    if len(translated) > 1:
        remaining = budget - len(html.escape(first, quote=False)) - 2
        if remaining >= 120:
            second = shorten(translated[1], remaining)
            while len(html.escape(second, quote=False)) > remaining:
                second = shorten(second, len(second) - 8)
            paragraphs.append(second)
    body = "\n\n".join(html.escape(p, quote=False) for p in paragraphs)
    caption = f"{header}\n\n{body}\n\n{hashtags}"
    # UTF-16 units also keep supplementary emoji within Telegram caption limits.
    if len(caption) >= 950 or len(caption.encode("utf-16-le")) // 2 >= 950:
        return None
    return caption


def publish(token, channel, photos, caption):
    endpoint = f"https://api.telegram.org/bot{token}/"
    if len(photos) == 1:
        method = "sendPhoto"
        data = {"chat_id": channel, "caption": caption, "parse_mode": "HTML"}
        files = {"photo": ("photo.jpg", photos[0], "image/jpeg")}
    else:
        method = "sendMediaGroup"
        media = []
        files = {}
        for index, photo in enumerate(photos):
            name = f"photo{index}"
            item = {"type": "photo", "media": f"attach://{name}"}
            if index == 0:
                item.update(caption=caption, parse_mode="HTML")
            media.append(item)
            files[name] = (f"{name}.jpg", photo, "image/jpeg")
        data = {"chat_id": channel, "media": json.dumps(media, ensure_ascii=False)}
    # Do not retry a send: a lost response could otherwise duplicate a published post.
    response = requests.post(endpoint + method, data=data, files=files, timeout=(15, 90))
    result = response.json()
    if response.status_code != 200 or not result.get("ok"):
        raise RuntimeError(f"Telegram rejected publication, code {result.get('error_code', response.status_code)}")


def main():
    history = load_history()
    missing = [name for name in ("TG_BOT_TOKEN", "TG_CHANNEL_ID") if not os.getenv(name)]
    if missing:
        print("Missing environment variables: " + ", ".join(missing))
        return 1
    translator = TranslationService()
    articles = collect_articles(set(history))
    print(f"Fresh, unseen, non-advertising articles: {len(articles)}")
    for _, feed_url, rss_title in articles:
        print(f"Checking: {feed_url}")
        try:
            url = feed_url
            if url in history:
                continue
            title, text, images = extract_article(url, rss_title)
            if ANTI_TRASH.search(title):
                continue
            photos = prepare_photos(images, url)
            if not photos:
                print("No suitable photos; moving to next article")
                continue
            caption = make_caption(translator, title, text)
            if not caption:
                print("Translation empty or invalid; moving to next article")
                continue
        except Exception as exc:
            print(f"Article processing failed ({type(exc).__name__})")
            continue
        try:
            publish(os.environ["TG_BOT_TOKEN"], os.environ["TG_CHANNEL_ID"], photos, caption)
        except Exception as exc:
            print(f"Publication failed or unconfirmed ({type(exc).__name__}); no retries this run")
            return 1
        save_history(history + [url])
        print(f"Published successfully: {url} ({len(photos)} photos)")
        return 0
    print("No suitable news found; nothing was published")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        # Do not print exception text: HTTP exceptions can include secret tokens.
        print(f"Bot stopped ({type(exc).__name__}); check configuration and posted.json")
        sys.exit(1)

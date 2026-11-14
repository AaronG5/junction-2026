"""Telegram preview parser, against trimmed real pages and small synthetic cases.

Fixtures (tests/fixtures/telegram/) are real t.me/s pages scraped 2026-11-14,
cut down to a few messages:
- SolovievLive.html: a reply, a forward, a photo with caption, a plain post
  and a media-only post ("Please open Telegram to view this post").
- InfoDefALL.html: a service message ("channel created") and two posts.
None of the scraped channels signed their posts, so signatures are synthetic.
"""
import re
import unittest
from pathlib import Path

from bs4 import BeautifulSoup

from scrapers.telegram.parse import message_id, parse_page

FIXTURES = Path(__file__).parent / "fixtures" / "telegram"
FIELDS = {"author", "content", "dateTime", "channelName", "postUrl"}
ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def fixture(name):
    return (FIXTURES / name).read_text(encoding="utf-8")


def by_url(page):
    return {p["postUrl"]: p for p in page.posts}


class RealPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = fixture("SolovievLive.html")
        cls.page = parse_page(cls.html, "СОЛОВЬЁВ")
        cls.posts = by_url(cls.page)

    def test_every_post_has_exactly_the_five_fields(self):
        self.assertEqual(len(self.page.posts), 5)
        for post in self.page.posts:
            self.assertEqual(set(post), FIELDS)
            self.assertEqual(post["author"], "СОЛОВЬЁВ")
            self.assertEqual(post["channelName"], "СОЛОВЬЁВ")
            self.assertRegex(post["postUrl"], r"^https://t\.me/SolovievLive/\d+$")
            self.assertRegex(post["dateTime"], ISO_UTC)
            self.assertNotIn("<br", post["content"])

    def test_values_of_one_post(self):
        post = self.posts["https://t.me/SolovievLive/392627"]
        self.assertEqual(post["dateTime"], "2026-11-14T06:39:17Z")
        self.assertTrue(post["content"].startswith("7️⃣8️⃣9️⃣"))   # emoji kept
        self.assertIn("\n\n", post["content"])                      # <br> -> newline

    def test_reply_content_excludes_the_quoted_message(self):
        soup = BeautifulSoup(self.html, "html.parser")
        reply = soup.select_one('div.tgme_widget_message[data-post="SolovievLive/392619"]')
        quoted = reply.select_one(".tgme_widget_message_reply .tgme_widget_message_text")
        quote = quoted.get_text(strip=True)[:40]
        content = self.posts["https://t.me/SolovievLive/392619"]["content"]
        self.assertTrue(content)
        self.assertNotIn(quote, content)

    def test_forwarded_post_author_is_the_posting_channel(self):
        post = self.posts["https://t.me/SolovievLive/392627"]
        self.assertIn("tgme_widget_message_forwarded_from", self.html)
        self.assertEqual(post["author"], "СОЛОВЬЁВ")
        self.assertEqual(post["postUrl"], "https://t.me/SolovievLive/392627")

    def test_media_only_post_is_kept_with_empty_content(self):
        self.assertEqual(self.posts["https://t.me/SolovievLive/392629"]["content"], "")

    def test_service_messages_are_skipped_but_count_as_messages(self):
        page = parse_page(fixture("InfoDefALL.html"), "InfoDefense hub (channel list)")
        self.assertEqual(page.message_ids, [1, 3, 4])
        self.assertEqual([p["postUrl"] for p in page.posts],
                         ["https://t.me/InfoDefAll/3", "https://t.me/InfoDefAll/4"])
        self.assertTrue(page.has_messages)

    def test_landing_page_has_no_messages(self):
        page = parse_page("<html><title>Telegram: View @x</title><body>Preview channel</body></html>", "x")
        self.assertFalse(page.has_messages)
        self.assertEqual(page.posts, [])


def message(body, *, data_post="chan/7", footer_extra="",
            date='<time datetime="2026-11-14T12:30:00+03:00" class="time">12:30</time>',
            owner='<a class="tgme_widget_message_owner_name" href="https://t.me/chan"><span dir="auto">Chan</span></a>'):
    """Markup shaped like a real t.me/s message."""
    return f"""
<div class="tgme_widget_message_wrap js-widget_message_wrap">
 <div class="tgme_widget_message text_not_supported_wrap js-widget_message" data-post="{data_post}">
  <div class="tgme_widget_message_bubble">
   <div class="tgme_widget_message_author accent_color">{owner}</div>
   {body}
   <div class="tgme_widget_message_footer compact js-message_footer"><div class="tgme_widget_message_info short js-message_info">
    <span class="tgme_widget_message_meta">{footer_extra}<a class="tgme_widget_message_date" href="https://t.me/{data_post}">{date}</a></span>
   </div></div>
  </div>
 </div>
</div>"""


class SyntheticTests(unittest.TestCase):
    def test_signed_post_uses_the_signature_as_author(self):
        html = message('<div class="tgme_widget_message_text js-message_text">hi</div>',
                       footer_extra='<span class="tgme_widget_message_from_author" dir="auto">Ivan Petrov</span>, ')
        self.assertEqual(parse_page(html, "Chan").posts[0]["author"], "Ivan Petrov")

    def test_text_cleanup(self):
        html = message('<div class="tgme_widget_message_text js-message_text" dir="auto">'
                       '  Tom &amp; Jerry <i class="emoji"><b>🔥</b></i><br/>see '
                       '<a href="https://example.com/x" target="_blank">this link</a>  </div>')
        post = parse_page(html, "Chan Name").posts[0]
        self.assertEqual(post["content"], "Tom & Jerry 🔥\nsee this link")
        self.assertEqual(post["dateTime"], "2026-11-14T09:30:00Z")     # +03:00 -> UTC
        self.assertEqual(post["channelName"], "Chan Name")
        self.assertEqual(post["postUrl"], "https://t.me/chan/7")

    def test_missing_date_is_null(self):
        post = parse_page(message("", date=""), "Chan").posts[0]
        self.assertIsNone(post["dateTime"])

    def test_author_falls_back_to_page_title_then_channel_name(self):
        no_owner = message("", owner="")
        titled = f'<div class="tgme_channel_info_header_title"><span>Page Title</span></div>{no_owner}'
        self.assertEqual(parse_page(titled, "Chan").posts[0]["author"], "Page Title")
        self.assertEqual(parse_page(no_owner, "Chan").posts[0]["author"], "Chan")

    def test_message_id(self):
        self.assertEqual(message_id("infalt/1234"), 1234)
        self.assertEqual(message_id("https://t.me/infalt/1234"), 1234)
        self.assertIsNone(message_id("infalt"))


if __name__ == "__main__":
    unittest.main()

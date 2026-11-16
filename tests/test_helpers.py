import datetime
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from bringmail import helpers

FIXTURE = os.path.join(os.path.dirname(__file__), 'fixtures', 'simple.eml')


class HelpersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(FIXTURE, 'rb') as handle:
            cls.raw = handle.read()

    def test_parse_full_message(self):
        (message, meta) = helpers.parse_full_message(self.raw)
        self.assertEqual(meta['subject'], 'Hello ✓')
        self.assertEqual(meta['from'][0]['name'], 'Alice Example')
        self.assertEqual(meta['from'][0]['addr'], 'alice@example.com')
        self.assertEqual(
            meta['date_unix'],
            int(datetime.datetime(2026, 1, 1, 12, 34, 56, tzinfo=datetime.timezone.utc).timestamp()),
        )
        self.assertEqual(meta['message_id'], '<simple-1@example.com>')
        self.assertTrue(meta['has_attachments'])
        self.assertTrue(meta['has_html'])
        self.assertTrue(meta['has_plain'])
        self.assertIn('Hello plain text.', meta['snippet'])
        self.assertEqual(len(meta['attachments']), 1)
        self.assertEqual(meta['attachments'][0]['filename'], 'notes.txt')

    def test_body_for_message(self):
        body = helpers.body_for_message(self.raw)
        self.assertIn('Hello <b>HTML</b> text.', body['html'])
        self.assertNotIn('<script', body['html'])
        self.assertIn('Hello plain text.', body['text'])
        self.assertEqual(len(body['attachments']), 1)
        self.assertFalse(body['remote_content'])
        self.assertIn('Subject:', body['headers'])

    def test_html_only_body_keeps_paragraphs(self):
        raw = (
            b'From: a@example.com\r\n'
            b'Subject: html only\r\n'
            b'Content-Type: text/html; charset=utf-8\r\n'
            b'\r\n'
            b'<html><body>'
            b'<p>First paragraph.</p>'
            b'<p>Second paragraph.</p>'
            b'<ul><li>One</li><li>Two</li></ul>'
            b'</body></html>'
        )
        body = helpers.body_for_message(raw)
        self.assertIn('First paragraph.\n\nSecond paragraph.', body['text'])
        self.assertIn('- One', body['text'])
        self.assertIn('- Two', body['text'])

    def test_normalize_plain_text(self):
        text = helpers.normalize_plain_text(
            '  first line\r\n\r\n\r\nsecond line  \r\n'
        )
        self.assertEqual(text, 'first line\n\nsecond line')

    def test_sanitizer(self):
        dirty = '''
        <p onclick="evil()">hello
        <a href="javascript:evil()">bad</a>
        <a href="https://example.com">good</a>
        <script>evil()</script>
        <img src="https://tracker.example.com/pixel.gif">
        </p>
        '''
        clean = helpers.sanitize_email_html(dirty)
        self.assertNotIn('<script', clean)
        self.assertNotIn('onclick', clean)
        self.assertNotIn('javascript:', clean)
        self.assertIn('https://example.com', clean)
        self.assertTrue(helpers.has_remote_content(clean))
        self.assertNotIn('tracker', helpers.sanitize_email_html('<script>x</script>'))

    def test_part_payload(self):
        # Leaves in walk order: text/plain alternative (0), text/html
        # alternative (1), notes.txt attachment (2).
        part = helpers.part_payload(self.raw, 2)
        self.assertEqual(part['filename'], 'notes.txt')
        self.assertIn(b'Attachment contents.', part['bytes'])

    def test_folder_special_use_fallbacks(self):
        from bringmail import constants
        self.assertEqual(
            helpers.folder_special_use((), 'INBOX'),
            constants.SPECIAL_USE_INBOX,
        )
        self.assertEqual(
            helpers.folder_special_use((), '[Gmail]/Trash'),
            constants.SPECIAL_USE_TRASH,
        )
        self.assertEqual(
            helpers.folder_special_use((b'\\Sent',), 'Gesendet'),
            constants.SPECIAL_USE_SENT,
        )
        self.assertEqual(
            helpers.folder_special_use((), 'INBOX.Deleted Items'),
            constants.SPECIAL_USE_TRASH,
        )
        self.assertEqual(
            helpers.folder_display_name('INBOX', '/'),
            'Inbox',
        )
        self.assertEqual(
            helpers.folder_display_name('INBOX.Subfolder', b'.'),
            'Subfolder',
        )
        self.assertEqual(helpers.folder_depth('INBOX.Subfolder', b'.'), 1)
        self.assertEqual(
            helpers.folder_display_name('INBOX/Subfolder', b'/'),
            'Subfolder',
        )
        self.assertEqual(helpers.folder_depth('INBOX/Subfolder', b'/'), 1)

    def test_date_fallback(self):
        internaldate = datetime.datetime(2025, 5, 4, 3, 2, 1, tzinfo=datetime.timezone.utc)
        raw = (
            b'From: a@example.com\r\n'
            b'Subject: no date\r\n'
            b'Content-Type: text/plain\r\n'
            b'\r\n'
            b'body\r\n'
        )
        (message, meta) = helpers.parse_full_message(raw, internaldate=internaldate)
        self.assertEqual(meta['date_unix'], int(internaldate.timestamp()))


if __name__ == '__main__':
    unittest.main()

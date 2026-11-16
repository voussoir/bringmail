import bs4
import contextlib
import datetime
import email
import email.header
import email.parser
import email.policy
import email.utils
import html as html_module
import imapclient
import json
import lxml.html
import re
import threading
import time
import typing

from . import constants
from . import exceptions

from voussoirkit import stringtools
from voussoirkit import vlogging

log = vlogging.get_logger(__name__)
vlogging.getLogger('imapclient').setLevel(vlogging.WARNING)


####################################################################################################
# TIME

def now(timestamp=True):
    '''
    Return the current UTC timestamp or datetime object.
    '''
    n = datetime.datetime.now(datetime.timezone.utc)
    if timestamp:
        return n.timestamp()
    return n


def datetime_to_timestamp(value):
    if value is None:
        return None
    if not isinstance(value, datetime.datetime):
        raise TypeError(value)
    if value.tzinfo is None:
        value = value.replace(tzinfo=datetime.timezone.utc)
    return int(value.timestamp())


def email_date_timestamp(message, fallback=None):
    '''
    Extract the Date header as a UTC epoch integer. If the header is missing
    or unparseable, return the fallback (usually the IMAP INTERNALDATE).
    '''
    header = message.get('Date')
    if header is None:
        return fallback

    value = getattr(header, 'datetime', None)
    if value is None:
        try:
            value = email.utils.parsedate_to_datetime(str(header))
        except (TypeError, ValueError):
            return fallback

    try:
        return datetime_to_timestamp(value)
    except (OverflowError, OSError, ValueError):
        return fallback


####################################################################################################
# NORMALIZERS

def normalize_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        return stringtools.truthystring(value)
    raise TypeError(value)


def normalize_int(value, *, minimum=None, maximum=None):
    if isinstance(value, bool):
        raise TypeError(value)
    if isinstance(value, float):
        value = int(value)
    if isinstance(value, str):
        value = int(value)
    if not isinstance(value, int):
        raise TypeError(value)
    if minimum is not None and value < minimum:
        raise ValueError(value)
    if maximum is not None and value > maximum:
        raise ValueError(value)
    return value


def normalize_string_blank_to_none(string):
    if string is None:
        return None
    if not isinstance(string, str):
        raise TypeError(string)
    string = string.strip()
    if not string:
        return None
    return string


def normalize_string_not_blank(string):
    if not isinstance(string, str):
        raise TypeError(string)
    string = string.strip()
    if not string:
        raise ValueError(string)
    return string


def normalize_string_strip(string):
    if not isinstance(string, str):
        raise TypeError(string)
    return string.strip()


####################################################################################################
# HEADERS AND ADDRESSES

def header_value(message, name):
    value = message.get(name)
    if value is None:
        return None
    return str(value)


def _address_to_dict(address):
    return {
        'name': address.display_name or None,
        'addr': address.addr_spec or None,
    }


def header_addresses(message, name):
    header = message.get(name)
    if header is None:
        return []

    addresses = getattr(header, 'addresses', None)
    if addresses is not None:
        return [_address_to_dict(address) for address in addresses]

    try:
        pairs = email.utils.getaddresses([str(header)])
    except Exception:
        return []

    result = []
    for (display_name, address) in pairs:
        result.append({
            'name': display_name or None,
            'addr': address or None,
        })
    return result


MESSAGE_ID_RE = re.compile(r'<[^<>]+>')


def header_message_ids(value):
    if value is None:
        return []
    return MESSAGE_ID_RE.findall(str(value))


####################################################################################################
# MIME PARTS

def iter_leaf_parts(message):
    '''
    Yield (part_index, part) for every non-multipart leaf in walk order.

    The index is stable for a given raw message and is used as the attachment
    identifier in URLs.
    '''
    index = 0
    for part in message.walk():
        if part.get_content_maintype() == 'multipart':
            continue
        yield (index, part)
        index += 1


def is_attachment_part(part):
    return part.get_content_disposition() == 'attachment'


def part_payload_bytes(part):
    payload = part.get_payload(decode=True)
    if payload is not None:
        return payload
    payload = part.get_payload(decode=False)
    if isinstance(payload, str):
        return payload.encode('utf-8', 'replace')
    return b''


def part_text(part):
    '''
    Decode a text part using its declared charset. Unknown charsets fall back
    to UTF-8 with replacement rather than raising LookupError.
    '''
    try:
        return part.get_content()
    except Exception:
        data = part_payload_bytes(part)
        charset = part.get_content_charset() or 'utf-8'
        try:
            return data.decode(charset, 'replace')
        except LookupError:
            return data.decode('utf-8', 'replace')


def find_text_part(message, subtype):
    for part in message.walk():
        if part.get_content_maintype() == 'multipart':
            continue
        if is_attachment_part(part):
            continue
        if part.get_content_type() == f'text/{subtype}':
            return part
    return None


def content_id_of(part):
    value = part.get('Content-ID')
    if value is None:
        return None
    value = str(value).strip()
    value = value.strip('<>').strip()
    return value or None


def text_from_html(html):
    if not html:
        return ''
    soup = bs4.BeautifulSoup(html, 'html.parser')
    for chaff in soup.find_all(['script', 'style', 'head', 'title', 'noscript']):
        chaff.decompose()
    text = soup.get_text(' ')
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def snippet_from_message(message):
    plain = find_text_part(message, 'plain')
    if plain is not None:
        text = part_text(plain)
    else:
        html_part = find_text_part(message, 'html')
        if html_part is None:
            return ''
        text = text_from_html(part_text(html_part))
    text = re.sub(r'\s+', ' ', text).strip()
    return text[:280]


####################################################################################################
# META

def _part_meta(index, part):
    payload = part_payload_bytes(part)
    return {
        'index': index,
        'content_type': part.get_content_type(),
        'charset': part.get_content_charset(),
        'filename': str(part.get_filename()) if part.get_filename() else None,
        'content_id': content_id_of(part),
        'disposition': part.get_content_disposition(),
        'size': len(payload),
    }


def build_meta(message, *, internaldate=None, size=None, include_body=True):
    '''
    Build the JSON-serializable metadata dictionary which sits beside the raw
    message in the database. The generated columns project the subject, sender,
    date, message-id and attachment flag out of this dictionary.
    '''
    internaldate_unix = datetime_to_timestamp(internaldate)
    date_unix = email_date_timestamp(message, fallback=internaldate_unix)
    if date_unix is None:
        # The generated date_unix column drives sorting and pagination, so keep
        # it non-null even for messages with no usable date at all.
        date_unix = 0

    meta = {
        'meta_version': constants.META_VERSION,
        'subject': header_value(message, 'Subject'),
        'from': header_addresses(message, 'From'),
        'sender': header_addresses(message, 'Sender'),
        'reply_to': header_addresses(message, 'Reply-To'),
        'to': header_addresses(message, 'To'),
        'cc': header_addresses(message, 'Cc'),
        'bcc': header_addresses(message, 'Bcc'),
        'date_header': header_value(message, 'Date'),
        'date_unix': date_unix,
        'internaldate_unix': internaldate_unix,
        'message_id': header_value(message, 'Message-ID'),
        'in_reply_to': header_value(message, 'In-Reply-To'),
        'references': header_message_ids(header_value(message, 'References')),
        'list_id': header_value(message, 'List-Id'),
        'size': size,
        'snippet': '',
        'has_html': False,
        'has_plain': False,
        'has_attachments': False,
        'parts': [],
        'attachments': [],
    }

    if not include_body:
        return meta

    parts = []
    attachments = []
    has_html = False
    has_plain = False

    for (index, part) in iter_leaf_parts(message):
        part_info = _part_meta(index, part)
        parts.append(part_info)

        content_type = part.get_content_type()
        if content_type == 'text/html' and not is_attachment_part(part):
            has_html = True
        elif content_type == 'text/plain' and not is_attachment_part(part):
            has_plain = True

        if part_info['filename'] or is_attachment_part(part):
            attachments.append({
                'index': index,
                'filename': part_info['filename'],
                'content_type': content_type,
                'size': part_info['size'],
                'content_id': part_info['content_id'],
                'disposition': part_info['disposition'],
            })

    meta['parts'] = parts
    meta['attachments'] = attachments
    meta['has_attachments'] = bool(attachments)
    meta['has_html'] = has_html
    meta['has_plain'] = has_plain
    meta['snippet'] = snippet_from_message(message)

    return meta


def parse_full_message(raw_bytes, *, internaldate=None, size=None):
    if not isinstance(raw_bytes, (bytes, bytearray)):
        raise TypeError(raw_bytes)
    try:
        message = email.parser.BytesParser(policy=email.policy.default).parsebytes(raw_bytes)
    except Exception as exc:
        raise exceptions.BadEmail(str(exc))

    if size is None:
        size = len(raw_bytes)
    meta = build_meta(message, internaldate=internaldate, size=size, include_body=True)
    return (message, meta)


def parse_header_message(raw_bytes, *, internaldate=None, size=None):
    if not isinstance(raw_bytes, (bytes, bytearray)):
        raise TypeError(raw_bytes)
    try:
        message = email.parser.BytesParser(policy=email.policy.default).parsebytes(raw_bytes)
    except Exception as exc:
        raise exceptions.BadEmail(str(exc))

    if size is None:
        size = len(raw_bytes)
    meta = build_meta(message, internaldate=internaldate, size=size, include_body=False)
    return (message, meta)


def headers_text(message):
    lines = []
    for (key, value) in message.items():
        lines.append(f'{key}: {value}')
    return '\n'.join(lines)


####################################################################################################
# HTML SANITIZATION

ALLOWED_TAGS = {
    'a', 'abbr', 'acronym', 'address', 'article', 'aside', 'audio', 'b', 'bdi', 'bdo',
    'big', 'blockquote', 'br', 'caption', 'center', 'cite', 'code', 'col', 'colgroup',
    'dd', 'del', 'details', 'dfn', 'div', 'dl', 'dt', 'em', 'figcaption', 'figure',
    'font', 'footer', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'header', 'hr', 'i', 'img',
    'ins', 'kbd', 'li', 'main', 'mark', 'ol', 'p', 'pre', 'q', 's', 'samp', 'section',
    'small', 'source', 'span', 'strike', 'strong', 'sub', 'summary', 'sup', 'table',
    'tbody', 'td', 'tfoot', 'th', 'thead', 'tr', 'tt', 'u', 'ul', 'var', 'video',
}

DROP_WITH_CONTENT = {
    'applet', 'base', 'button', 'canvas', 'embed', 'form', 'frame', 'frameset', 'head',
    'iframe', 'input', 'link', 'math', 'meta', 'noscript', 'object', 'option', 'script',
    'select', 'textarea', 'title',
}

ALLOWED_ATTRIBUTES = {
    'align', 'alt', 'background', 'bgcolor', 'border', 'cellpadding', 'cellspacing',
    'charset', 'class', 'color', 'colspan', 'controls', 'dir', 'face', 'height', 'href',
    'id', 'lang', 'poster', 'rowspan', 'size', 'src', 'srcset', 'start', 'style',
    'target', 'title', 'type', 'valign', 'width',
}

URL_ATTRIBUTES = {'href', 'src', 'background', 'poster'}

CSS_BAD_PATTERNS = (
    'expression(',
    'javascript:',
    '-moz-binding',
    'behavior:',
)

STYLE_TAG_BAD_PATTERNS = (
    '@import',
    'expression(',
    'javascript:',
    '-moz-binding',
)


def _url_is_safe(url):
    if url is None:
        return False
    url = str(url).strip()
    if not url:
        return False

    # Strip control characters that browsers ignore.
    url = re.sub(r'[\x00-\x20\x7f]+', '', url)
    lower = url.lower()

    if lower.startswith('javascript:') or lower.startswith('vbscript:'):
        return False
    if lower.startswith('data:'):
        return lower.startswith('data:image/')
    if lower.startswith(('http:', 'https:', 'mailto:', 'cid:', 'tel:')):
        return True
    if url.startswith(('#', '/', './', '../')):
        return True
    # Relative URLs without a scheme.
    if ':' not in url.split('/', 1)[0]:
        return True
    return False


def _style_is_safe(style):
    if not style:
        return True
    lower = str(style).lower()
    return not any(pattern in lower for pattern in CSS_BAD_PATTERNS)


def sanitize_email_html(html):
    '''
    Sanitize untrusted email HTML with an allowlist. The reader still renders
    this inside a sandboxed iframe with a restrictive CSP, so this is the
    first of two layers.
    '''
    if not html:
        return ''

    try:
        document = lxml.html.document_fromstring(html)
    except Exception:
        return '<pre>' + html_module.escape(html) + '</pre>'

    style_texts = []
    for style in list(document.xpath('//style')):
        text = style.text or ''
        lower = text.lower()
        if not any(pattern in lower for pattern in STYLE_TAG_BAD_PATTERNS):
            style_texts.append(f'<style>{text}</style>')
        style.drop_tree()

    for comment in list(document.xpath('//comment()')):
        if comment.getparent() is not None:
            comment.getparent().remove(comment)

    body = document.find('body')
    container = body if body is not None else document

    for element in list(container.iterdescendants()):
        if element.getparent() is None:
            # An ancestor (iframe, form, ...) was already dropped with its
            # entire subtree.
            continue

        if not isinstance(element.tag, str):
            element.drop_tree()
            continue

        tag = element.tag.lower()
        if tag in DROP_WITH_CONTENT:
            element.drop_tree()
            continue
        if tag not in ALLOWED_TAGS:
            element.drop_tag()
            continue

        for (attribute, value) in list(element.attrib.items()):
            attribute_lower = attribute.lower()

            if attribute_lower.startswith('on'):
                del element.attrib[attribute]
                continue

            if attribute_lower in URL_ATTRIBUTES and not _url_is_safe(value):
                del element.attrib[attribute]
                continue

            if attribute_lower == 'style':
                if _style_is_safe(value):
                    continue
                del element.attrib[attribute]
                continue

            if attribute_lower == 'target':
                continue

            if attribute_lower not in ALLOWED_ATTRIBUTES:
                del element.attrib[attribute]

        if tag == 'a':
            if 'href' not in element.attrib:
                pass
            else:
                element.attrib['rel'] = 'noopener noreferrer'
                element.attrib['target'] = '_blank'

    inner = ''.join(
        lxml.html.tostring(child, encoding='unicode', method='html')
        for child in container
    )
    if style_texts:
        return '\n'.join(style_texts) + '\n' + inner
    return inner


REMOTE_URL_RE = re.compile(
    r'''(?:src|srcset|background|poster)\s*=\s*["']?\s*(?:https?:)?//''',
    flags=re.IGNORECASE,
)
CSS_URL_RE = re.compile(
    r'''url\(\s*["']?\s*(?:https?:)?//''',
    flags=re.IGNORECASE,
)


def has_remote_content(html):
    if not html:
        return False
    return bool(REMOTE_URL_RE.search(html) or CSS_URL_RE.search(html))


####################################################################################################
# READER PAYLOAD

def inline_image_map(message):
    mapping = {}
    for (index, part) in iter_leaf_parts(message):
        if not part.get_content_type().startswith('image/'):
            continue
        content_id = content_id_of(part)
        if content_id:
            mapping[content_id] = index
    return mapping


def body_for_message(raw_bytes):
    if not raw_bytes:
        raise exceptions.MessageNotCached('')

    (message, meta) = parse_full_message(raw_bytes)

    plain = find_text_part(message, 'plain')
    html_part = find_text_part(message, 'html')

    plain_text = part_text(plain) if plain is not None else ''
    html = part_text(html_part) if html_part is not None else ''

    if not plain_text and html:
        plain_text = text_from_html(html)

    sanitized = sanitize_email_html(html)
    remote_content = has_remote_content(sanitized)

    return {
        'subject': meta['subject'],
        'from': meta['from'],
        'to': meta['to'],
        'cc': meta['cc'],
        'date_unix': meta['date_unix'],
        'message_id': meta['message_id'],
        'html': sanitized,
        'text': plain_text,
        'headers': headers_text(message),
        'inline_images': inline_image_map(message),
        'remote_content': remote_content,
        'attachments': meta['attachments'],
    }


def part_payload(raw_bytes, part_index):
    (message, meta) = parse_full_message(raw_bytes)
    for (index, part) in iter_leaf_parts(message):
        if index != part_index:
            continue
        return {
            'bytes': part_payload_bytes(part),
            'content_type': part.get_content_type(),
            'filename': part.get_filename(),
            'disposition': part.get_content_disposition(),
            'content_id': content_id_of(part),
        }
    raise exceptions.NoSuchPart(part_index)


####################################################################################################
# IMAP

def connect_imap(account):
    '''
    Open and authenticate an IMAP connection. The caller owns the connection
    and is responsible for logging out.
    '''
    try:
        client = imapclient.IMAPClient(
            host=account.host,
            port=account.port,
            ssl=account.ssl,
            timeout=imapclient.SocketTimeout(
                connect=constants.IMAP_CONNECT_TIMEOUT,
                read=constants.IMAP_READ_TIMEOUT,
            ),
        )
    except Exception as exc:
        raise exceptions.MailServerConnectionError(str(exc))

    try:
        client.normalise_times = False
        if account.starttls and not account.ssl:
            client.starttls()
        client.login(account.username, account.password)
    except Exception as exc:
        with contextlib.suppress(Exception):
            client.shutdown()
        raise exceptions.MailServerLoginError(str(exc))

    return client


@contextlib.contextmanager
def imap_connection(account, *, folder=None, readonly=False):
    client = connect_imap(account)
    try:
        if folder is not None:
            client.select_folder(folder, readonly=readonly)
        yield client
    finally:
        with contextlib.suppress(Exception):
            client.logout()


def enable_condstore(client):
    if not client.has_capability('CONDSTORE'):
        return False
    try:
        if client.has_capability('ENABLE'):
            client.enable('CONDSTORE')
    except Exception:
        log.debug('Could not ENABLE CONDSTORE, continuing without it.')
    return True


# Authenticated sessions are kept open across message actions so that clicking
# through a mailbox does not perform a full IMAP login for every read / flag /
# move. Sync itself uses its own short-lived connection.
_IMAP_SESSIONS = {}
_IMAP_SESSION_LOCKS = {}
_IMAP_SESSIONS_LOCK = threading.Lock()


def _imap_session_key(account):
    return (
        account.host,
        account.port,
        bool(account.ssl),
        bool(account.starttls),
        account.username,
        account.password,
    )


def _imap_session_lock(account_id):
    with _IMAP_SESSIONS_LOCK:
        lock = _IMAP_SESSION_LOCKS.get(account_id)
        if lock is None:
            lock = threading.RLock()
            _IMAP_SESSION_LOCKS[account_id] = lock
        return lock


def drop_imap_session(account_id):
    lock = _imap_session_lock(account_id)
    with lock:
        with _IMAP_SESSIONS_LOCK:
            entry = _IMAP_SESSIONS.pop(account_id, None)
        if entry is not None:
            try:
                entry['client'].logout()
            except Exception:
                try:
                    entry['client'].shutdown()
                except Exception:
                    pass


def imap_call(account, function):
    '''
    Run function(client) on a reusable authenticated connection for this
    account. The per-account lock serializes operations on that connection;
    different accounts can still operate in parallel.

    If the operation raises, the session is discarded so the next call starts
    with a fresh login. We deliberately do not retry inside this function,
    because retrying a MOVE or STORE after an ambiguous network failure could
    apply it twice.
    '''
    lock = _imap_session_lock(account.id)
    with lock:
        client = None
        stale = None
        last_used = 0
        with _IMAP_SESSIONS_LOCK:
            entry = _IMAP_SESSIONS.get(account.id)
            if entry is not None and entry['key'] != _imap_session_key(account):
                stale = _IMAP_SESSIONS.pop(account.id)
                entry = None
            if entry is not None:
                client = entry['client']
                last_used = entry.get('last_used', 0)

        if stale is not None:
            try:
                stale['client'].logout()
            except Exception:
                pass

        if client is not None:
            # A long-idle connection may already be dead. NOOP before we
            # attempt a real action so a stale socket does not surface as a
            # failed read or flag change.
            if time.monotonic() - last_used > 60:
                try:
                    client.noop()
                except Exception:
                    drop_imap_session(account.id)
                    client = None

        if client is None:
            client = connect_imap(account)
            with _IMAP_SESSIONS_LOCK:
                _IMAP_SESSIONS[account.id] = {
                    'key': _imap_session_key(account),
                    'client': client,
                    'last_used': time.monotonic(),
                }

        try:
            result = function(client)
        except Exception:
            drop_imap_session(account.id)
            raise

        with _IMAP_SESSIONS_LOCK:
            entry = _IMAP_SESSIONS.get(account.id)
            if entry is not None and entry['client'] is client:
                entry['last_used'] = time.monotonic()
        return result


def decode_flag(flag):
    if isinstance(flag, bytes):
        return flag.decode('ascii', 'replace')
    return str(flag)


def flags_to_booleans(flags):
    flags = {decode_flag(flag) for flag in flags}
    return {
        'seen': '\\Seen' in flags,
        'flagged': '\\Flagged' in flags,
        'answered': '\\Answered' in flags,
        'imap_deleted': '\\Deleted' in flags,
        'draft': '\\Draft' in flags,
    }


def booleans_to_flags(**booleans):
    mapping = {
        'seen': b'\\Seen',
        'flagged': b'\\Flagged',
        'answered': b'\\Answered',
        'imap_deleted': b'\\Deleted',
        'draft': b'\\Draft',
    }
    return [flag for (name, flag) in mapping.items() if booleans.get(name)]


POPULAR_SPECIAL_FOLDER_NAMES = {
    constants.SPECIAL_USE_TRASH: {
        'trash', 'deleted', 'deleted items', 'deleted messages', 'bin',
    },
    constants.SPECIAL_USE_SENT: {
        'sent', 'sent items', 'sent messages', 'sent mail',
    },
    constants.SPECIAL_USE_DRAFTS: {
        'drafts', 'draft',
    },
    constants.SPECIAL_USE_JUNK: {
        'junk', 'spam', 'bulk mail',
    },
    constants.SPECIAL_USE_ARCHIVE: {
        'archive', 'archives',
    },
}


def folder_special_use(flags, name):
    decoded = {decode_flag(flag) for flag in flags}
    for special_use in constants.SPECIAL_USES:
        if special_use == constants.SPECIAL_USE_INBOX:
            continue
        if special_use in decoded:
            return special_use
    if name.upper() == 'INBOX':
        return constants.SPECIAL_USE_INBOX

    # RFC 6154 special-use flags are optional. Fall back to the common names
    # used by servers that do not advertise them.
    last_segment = re.split(r'[./]', name)[-1].strip().lower()
    for (special_use, names) in POPULAR_SPECIAL_FOLDER_NAMES.items():
        if last_segment in names:
            return special_use
    return None


def folder_display_name(name, delimiter):
    if not delimiter:
        return name
    parts = [part for part in name.split(delimiter) if part]
    if not parts:
        return name
    return parts[-1]


def folder_depth(name, delimiter):
    if not delimiter:
        return 0
    return max(0, len([part for part in name.split(delimiter) if part]) - 1)


def folder_sort_key(name, special_use):
    priority = {
        constants.SPECIAL_USE_INBOX: 0,
        constants.SPECIAL_USE_DRAFTS: 1,
        constants.SPECIAL_USE_SENT: 2,
        constants.SPECIAL_USE_ARCHIVE: 3,
        constants.SPECIAL_USE_JUNK: 4,
        constants.SPECIAL_USE_TRASH: 9,
    }.get(special_use, 5)
    return (priority, name.lower())


####################################################################################################
# MISC

def maybe_int(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def total_size(nbytes):
    if nbytes is None:
        return ''
    nbytes = int(nbytes)
    for (suffix, scale) in [('GB', 1024 ** 3), ('MB', 1024 ** 2), ('KB', 1024)]:
        if nbytes >= scale:
            return f'{nbytes / scale:.1f} {suffix}'
    return f'{nbytes} B'

'''
IMAP IDLE protocol session.

This module is frontend-agnostic. It opens a dedicated authenticated
connection, selects one mailbox read-only, and translates the untagged
responses sent during IDLE into events. It does not start threads, write to the
database, or depend on Flask. The reusable thread/supervisor layer lives in
bringmail.idle_supervisor.
'''
import contextlib
import dataclasses
import imaplib
import ssl
import time

from . import constants
from . import exceptions
from . import helpers

from voussoirkit import vlogging

log = vlogging.get_logger(__name__)

EVENT_EXISTS = 'exists'
EVENT_EXPUNGE = 'expunge'
EVENT_FLAGS = 'flags'
EVENT_OK = 'ok'
EVENT_BYE = 'bye'
EVENT_OTHER = 'other'

# Socket closures that are a normal part of the IDLE lifecycle. Other errors,
# such as bad credentials, TLS verification, and DNS failures, must remain
# visible to the operator.
_EXPECTED_DISCONNECTS = (
    imaplib.IMAP4.abort,
    ConnectionAbortedError,
    ConnectionResetError,
    BrokenPipeError,
    ssl.SSLEOFError,
)


@dataclasses.dataclass(frozen=True)
class IdleEvent:
    '''
    One parsed untagged response received during IDLE.
    '''
    kind: str
    count: int | None = None
    flags: tuple[str, ...] = ()
    text: str | None = None
    raw: tuple = ()


def _decode(value):
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode('utf-8', 'replace')
    return str(value)


def _maybe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_response(response):
    '''
    Convert one response from IMAPClient.idle_check or idle_done into an
    IdleEvent.
    '''
    if not isinstance(response, tuple) or len(response) < 2:
        raw = response if isinstance(response, tuple) else (response,)
        return IdleEvent(EVENT_OTHER, raw=raw)

    (first, second) = (response[0], response[1])

    if isinstance(first, bytes):
        first_upper = first.upper()
        if first_upper == b'BYE':
            return IdleEvent(EVENT_BYE, text=_decode(second), raw=response)
        if first_upper == b'OK':
            return IdleEvent(EVENT_OK, text=_decode(second), raw=response)

    if isinstance(second, bytes):
        name = second.upper()
        if name == b'EXISTS':
            return IdleEvent(EVENT_EXISTS, count=_maybe_int(first), raw=response)
        if name == b'EXPUNGE':
            return IdleEvent(EVENT_EXPUNGE, count=_maybe_int(first), raw=response)
        if name == b'FETCH':
            flags = ()
            for extra in response[2:]:
                if not isinstance(extra, tuple) or len(extra) < 2:
                    continue
                if not isinstance(extra[0], bytes):
                    continue
                if extra[0].upper() != b'FLAGS':
                    continue
                values = extra[1]
                if isinstance(values, (list, tuple)):
                    flags = tuple(helpers.decode_flag(value) for value in values)
                break
            return IdleEvent(
                EVENT_FLAGS,
                count=_maybe_int(first),
                flags=flags,
                raw=response,
            )

    return IdleEvent(EVENT_OTHER, raw=response)


class IdleSession:
    '''
    One IMAP IDLE connection bound to one mailbox.

    The caller is responsible for polling this session. A session is not
    thread-safe; give each watcher thread its own instance.
    '''

    def __init__(
            self,
            account,
            folder_name,
            *,
            renew_seconds=constants.IDLE_RENEW_SECONDS,
            poll_seconds=constants.IDLE_POLL_SECONDS,
        ):
        if not folder_name:
            raise ValueError('folder_name is required')
        if renew_seconds <= 0:
            raise ValueError('renew_seconds must be positive')
        if poll_seconds <= 0:
            raise ValueError('poll_seconds must be positive')

        self.account = account
        self.folder_name = folder_name
        self.renew_seconds = renew_seconds
        self.poll_seconds = poll_seconds
        self.client = None
        self._idling = False
        self._idle_started = 0.0

    @property
    def is_open(self):
        return self.client is not None

    @property
    def is_idling(self):
        return self._idling

    @property
    def renew_due(self):
        if not self._idling:
            return False
        return (time.monotonic() - self._idle_started) >= self.renew_seconds

    @contextlib.contextmanager
    def _disconnect_guard(self):
        '''
        Translate a closed socket into IdleDisconnected so callers can tell it
        apart from configuration and protocol failures.
        '''
        try:
            yield
        except _EXPECTED_DISCONNECTS as exc:
            self._idling = False
            raise exceptions.IdleDisconnected(str(exc)) from exc

    @staticmethod
    def _close_client(client, *, graceful):
        if graceful:
            try:
                client.logout()
                return
            except Exception:
                log.debug('IDLE logout failed; shutting down instead.', exc_info=True)
        with contextlib.suppress(Exception):
            client.shutdown()

    def open(self):
        '''
        Connect, verify the IDLE capability, and select the mailbox read-only.
        Raises IdleUnsupported if the server does not advertise IDLE.
        '''
        if self.client is not None:
            return

        log.debug('Opening IDLE session for account %s.', getattr(self.account, 'id', None))
        client = helpers.connect_imap(self.account)
        try:
            if not client.has_capability('IDLE'):
                raise exceptions.IdleUnsupported()
            client.select_folder(self.folder_name, readonly=True)
        except Exception:
            self._close_client(client, graceful=False)
            raise

        self.client = client
        self._idling = False
        self._idle_started = 0.0

    def _start_idle(self):
        if self.client is None:
            raise RuntimeError('IdleSession is not open.')
        self.client.idle()
        self._idling = True
        self._idle_started = time.monotonic()

    def poll(self, timeout=None):
        '''
        Wait up to timeout seconds for IDLE responses and return parsed events.
        Starts IDLE on the first call and renews it when the renewal deadline
        has passed. Responses received while renewing are included in the
        return value.
        '''
        if self.client is None:
            raise RuntimeError('IdleSession is not open.')
        if timeout is None:
            timeout = self.poll_seconds

        with self._disconnect_guard():
            if not self._idling:
                self._start_idle()

            responses = self.client.idle_check(timeout=timeout)
            events = [parse_response(response) for response in (responses or [])]

            if self.renew_due:
                events.extend(self.renew())

        return events

    def renew(self):
        '''
        Terminate and re-issue IDLE so the server does not apply its
        inactivity timeout. Returns events received by idle_done.
        '''
        if self.client is None:
            raise RuntimeError('IdleSession is not open.')
        if not self._idling:
            return []

        with self._disconnect_guard():
            try:
                (_, responses) = self.client.idle_done()
            except Exception:
                self._idling = False
                raise

            self._idling = False
            events = [parse_response(response) for response in (responses or [])]
            self._start_idle()

        return events

    def close(self, graceful=True):
        '''
        Close the connection. Graceful closes terminate IDLE and logout.
        Failed or non-graceful closes shutdown the socket without pretending
        the session is usable.
        '''
        client = self.client
        self.client = None
        if client is None:
            return

        idling = self._idling
        self._idling = False

        if idling and graceful:
            try:
                client.idle_done()
            except Exception:
                log.debug('IDLE shutdown did not complete cleanly.', exc_info=True)
                graceful = False

        self._close_client(client, graceful=graceful)
        log.debug('Closed IDLE session for account %s.', getattr(self.account, 'id', None))

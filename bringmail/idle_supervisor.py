'''
Reusable IMAP IDLE runtime.

This module owns the watcher threads, reconnect policy, and reconciliation
against the database. It is frontend agnostic: callers provide on_change and
on_state callbacks and decide whether a change should enqueue a sync, update a
TUI, or notify a user.

The supervisor never starts at import time. A frontend or CLI must call
start() explicitly and stop() during shutdown.
'''
import dataclasses
import threading
import time

from . import constants
from . import exceptions
from . import helpers
from . import idle

from voussoirkit import vlogging

log = vlogging.get_logger(__name__)

STATE_CONNECTING = 'connecting'
STATE_CONNECTED = 'connected'
STATE_RETRYING = 'retrying'
STATE_UNSUPPORTED = 'unsupported'
STATE_STOPPED = 'stopped'

@dataclasses.dataclass(frozen=True)
class AccountSnapshot:
    '''
    A copy of the connection details a watcher needs. Holding the ORM objects
    across threads would expose mutable, cached state and keep the password
    alive in a place where it is easy to log accidentally.
    '''
    id: int
    display_name: str
    host: str
    port: int
    ssl: bool
    starttls: bool
    username: str
    password: str = dataclasses.field(repr=False)
    folder_id: int
    folder_name: str

    @classmethod
    def from_account_folder(cls, account, folder):
        return cls(
            id=account.id,
            display_name=account.display_name,
            host=account.host,
            port=account.port,
            ssl=account.ssl,
            starttls=account.starttls,
            username=account.username,
            password=account.password,
            folder_id=folder.id,
            folder_name=folder.name,
        )

def find_inbox_folder(account):
    '''
    Return the account's selectable INBOX folder, or None.
    '''
    for folder in account.get_folders():
        special_use = folder.special_use
        if not special_use:
            special_use = helpers.folder_special_use(folder.attributes, folder.name)
        if special_use != constants.SPECIAL_USE_INBOX:
            continue
        if not folder.is_selectable:
            continue
        return folder
    return None

class IdleWatcher:
    '''
    One thread and one IMAP connection watching one mailbox.

    The thread spends most of its time blocked in IdleSession.poll. The poll
    timeout is a local wake-up; it does not poll the server for mail.
    '''

    def __init__(
            self,
            snapshot,
            on_change,
            on_state,
            *,
            poll_seconds=constants.IDLE_POLL_SECONDS,
            renew_seconds=constants.IDLE_RENEW_SECONDS,
            debounce_seconds=constants.IDLE_DEBOUNCE_SECONDS,
            reconnect_min_seconds=constants.IDLE_RECONNECT_MIN_SECONDS,
            reconnect_max_seconds=constants.IDLE_RECONNECT_MAX_SECONDS,
            unsupported_retry_seconds=constants.IDLE_UNSUPPORTED_RETRY_SECONDS,
            session_factory=idle.IdleSession,
        ):
        self.snapshot = snapshot
        self.on_change = on_change
        self.on_state = on_state
        self.poll_seconds = poll_seconds
        self.renew_seconds = renew_seconds
        self.debounce_seconds = debounce_seconds
        self.reconnect_min_seconds = reconnect_min_seconds
        self.reconnect_max_seconds = reconnect_max_seconds
        self.unsupported_retry_seconds = unsupported_retry_seconds
        self.session_factory = session_factory

        self.state = STATE_STOPPED
        self.detail = None

        self._thread = None
        self._stop_event = threading.Event()
        self._pending_events = []
        self._next_emit_time = 0.0

    @property
    def is_alive(self):
        return bool(self._thread and self._thread.is_alive())

    def start(self, delay=0):
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            args=(delay,),
            name=f'bringmail-idle-{self.snapshot.id}',
            daemon=True,
        )
        self._thread.start()

    def stop(self):
        self._stop_event.set()

    def join(self, timeout=None):
        if self._thread is not None:
            self._thread.join(timeout=timeout)

    def _notify_state(self, state, detail=None):
        self.state = state
        self.detail = detail
        if self.on_state is None:
            return
        try:
            self.on_state(self.snapshot, state, detail)
        except Exception:
            log.debug('IDLE state callback raised.', exc_info=True)

    def _notify_change(self, events, reason):
        if self.on_change is None:
            return
        try:
            self.on_change(self.snapshot, list(events), reason)
        except Exception:
            log.debug('IDLE change callback raised.', exc_info=True)

    def _emit_pending(self):
        if not self._pending_events:
            return
        events = self._pending_events
        self._pending_events = []
        self._next_emit_time = time.monotonic() + self.debounce_seconds
        self._notify_change(events, 'idle')

    def _collect(self, events):
        '''
        Coalesce bursts of mailbox events into one change callback. The first
        event is emitted immediately; further events are held until the
        debounce window has elapsed.
        '''
        now = time.monotonic()
        if events:
            self._pending_events.extend(events)
            if now >= self._next_emit_time:
                self._emit_pending()
            return

        if self._pending_events and now >= self._next_emit_time:
            self._emit_pending()

    def _run(self, delay):
        if delay > 0 and self._stop_event.wait(delay):
            self._notify_state(STATE_STOPPED)
            return

        reconnect_seconds = self.reconnect_min_seconds

        while not self._stop_event.is_set():
            self._notify_state(STATE_CONNECTING)
            session = None
            graceful = False

            try:
                session = self.session_factory(
                    self.snapshot,
                    self.snapshot.folder_name,
                    renew_seconds=self.renew_seconds,
                    poll_seconds=self.poll_seconds,
                )
                session.open()
                self._notify_state(STATE_CONNECTED)

                # Events may have been missed while disconnected or while the
                # application was not running. The caller performs a targeted
                # sync for this folder before relying on IDLE notifications.
                self._notify_change([], 'connected')

                reconnect_seconds = self.reconnect_min_seconds
                graceful = True

                while not self._stop_event.is_set():
                    events = session.poll(timeout=self.poll_seconds)
                    if any(event.kind == idle.EVENT_BYE for event in events):
                        raise exceptions.IdleDisconnected(
                            'The server closed the IDLE connection.'
                        )
                    interesting = [
                        event for event in events
                        if event.kind in (
                            idle.EVENT_EXISTS,
                            idle.EVENT_EXPUNGE,
                            idle.EVENT_FLAGS,
                        )
                    ]
                    self._collect(interesting)

            except exceptions.IdleUnsupported:
                graceful = False
                self._notify_state(STATE_UNSUPPORTED, 'Server does not advertise IDLE.')
                log.info('IMAP IDLE is not supported by %s.', self.snapshot)
                if self._stop_event.wait(self.unsupported_retry_seconds):
                    break
                continue

            except Exception as exc:
                graceful = False
                self._notify_state(STATE_RETRYING, str(exc))
                if isinstance(exc, exceptions.IdleDisconnected):
                    # Server-side closes are a normal part of IDLE. Keep the
                    # default log quiet while retaining the detail for --debug.
                    log.debug(
                        'IMAP IDLE connection for %s disconnected: %s',
                        self.snapshot,
                        exc,
                    )
                else:
                    log.info(
                        'IMAP IDLE connection for %s failed: %s',
                        self.snapshot,
                        exc,
                    )
                if self._stop_event.wait(reconnect_seconds):
                    break
                reconnect_seconds = min(
                    reconnect_seconds * 2,
                    self.reconnect_max_seconds,
                )
                continue

            finally:
                if session is not None:
                    session.close(graceful=graceful)

        self._notify_state(STATE_STOPPED)

class IdleSupervisor:
    '''
    Reconcile the set of IdleWatcher threads with the accounts and INBOX
    folders in the database. Call start() once and reconfigure() after account
    changes or syncs that may have discovered new folders.
    '''

    def __init__(
            self,
            bringdb,
            on_change=None,
            on_state=None,
            *,
            poll_seconds=constants.IDLE_POLL_SECONDS,
            renew_seconds=constants.IDLE_RENEW_SECONDS,
            debounce_seconds=constants.IDLE_DEBOUNCE_SECONDS,
            reconnect_min_seconds=constants.IDLE_RECONNECT_MIN_SECONDS,
            reconnect_max_seconds=constants.IDLE_RECONNECT_MAX_SECONDS,
            unsupported_retry_seconds=constants.IDLE_UNSUPPORTED_RETRY_SECONDS,
            startup_stagger_seconds=constants.IDLE_STARTUP_STAGGER_SECONDS,
            watcher_class=IdleWatcher,
        ):
        self.bringdb = bringdb
        self.on_change = on_change
        self.on_state = on_state
        self.poll_seconds = poll_seconds
        self.renew_seconds = renew_seconds
        self.debounce_seconds = debounce_seconds
        self.reconnect_min_seconds = reconnect_min_seconds
        self.reconnect_max_seconds = reconnect_max_seconds
        self.unsupported_retry_seconds = unsupported_retry_seconds
        self.startup_stagger_seconds = startup_stagger_seconds
        self.watcher_class = watcher_class

        self._lock = threading.RLock()
        self._watchers = {}
        self._started = False
        self._stopping = False

    @property
    def is_started(self):
        with self._lock:
            return self._started

    def status(self):
        '''
        Return a thread-safe snapshot of watcher states keyed by account id.
        '''
        with self._lock:
            return {
                account_id: {
                    'account_id': watcher.snapshot.id,
                    'display_name': watcher.snapshot.display_name,
                    'folder_id': watcher.snapshot.folder_id,
                    'folder_name': watcher.snapshot.folder_name,
                    'state': watcher.state,
                    'detail': watcher.detail,
                }
                for (account_id, watcher) in self._watchers.items()
            }

    def start(self):
        with self._lock:
            if self._started:
                return
            self._started = True
            self._stopping = False
        self.reconfigure()

    def stop(self):
        with self._lock:
            self._stopping = True
            watchers = list(self._watchers.values())
            self._watchers.clear()

        for watcher in watchers:
            watcher.stop()
        for watcher in watchers:
            watcher.join(timeout=self.poll_seconds + 10)

        with self._lock:
            self._started = False
            self._stopping = False

    def run_forever(self, stop_event=None):
        '''
        Blocking convenience method for CLI or daemon callers. Web frontends
        normally call start() and let their own process lifecycle manage it.
        '''
        self.start()
        event = stop_event or threading.Event()
        try:
            while not event.wait(3600):
                pass
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def reconfigure(self):
        '''
        Start, stop, or replace watchers so they match the current database
        state. Accounts with no selectable INBOX are skipped until a later
        sync creates one.
        '''
        with self._lock:
            if not self._started or self._stopping:
                return

        desired = self._desired_snapshots()

        with self._lock:
            for account_id in list(self._watchers):
                if account_id not in desired:
                    watcher = self._watchers.pop(account_id)
                    watcher.stop()

            for (account_id, snapshot) in desired.items():
                current = self._watchers.get(account_id)
                if current is not None and current.snapshot == snapshot:
                    continue
                if current is not None:
                    current.stop()

                watcher = self.watcher_class(
                    snapshot,
                    self.on_change,
                    self.on_state,
                    poll_seconds=self.poll_seconds,
                    renew_seconds=self.renew_seconds,
                    debounce_seconds=self.debounce_seconds,
                    reconnect_min_seconds=self.reconnect_min_seconds,
                    reconnect_max_seconds=self.reconnect_max_seconds,
                    unsupported_retry_seconds=self.unsupported_retry_seconds,
                )
                self._watchers[account_id] = watcher
                watcher.start(
                    delay=(len(self._watchers) - 1) * self.startup_stagger_seconds
                )

    def _desired_snapshots(self):
        desired = {}
        try:
            accounts = list(self.bringdb.get_accounts())
        except Exception:
            log.warning('Could not read accounts for IMAP IDLE.', exc_info=True)
            return desired

        for account in accounts:
            if account.offline:
                # Offline accounts have no network connection to watch.
                continue
            try:
                folder = find_inbox_folder(account)
            except Exception:
                log.warning('Could not inspect folders for %s.', account, exc_info=True)
                continue
            if folder is None:
                continue
            desired[account.id] = AccountSnapshot.from_account_folder(account, folder)

        return desired

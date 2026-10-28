"""Opt-in ACP background drain; the caller owns deadlines and SSH cleanup."""
import queue
import threading
import time

from agent_background_air import AirBackground, CAPABILITIES, close_confirmed, supported


HEARTBEAT_KEY = 'qwen.daemon.activeWorkHeartbeat'
CATEGORIES = ['agent', 'notification', 'shell', 'workflow']
POLL_SECONDS = 1.0
CLEANUP_TIMEOUT_SECONDS = 5


class BackgroundACP:
    """Keep stdout draining between RPCs without invoking callbacks in the reader.

    The existing strict ACP parser still handles requests. Only this inbox reads
    stdout; notifications between requests are delivered by pump() in the same
    worker as normal ACP updates. Its bounded queue applies backpressure.
    """

    def __init__(self, stdout, cancelled, requested, decode, dispatch, raw_read, boundary):
        self.stdout = stdout
        self.cancelled = cancelled
        self.requested = requested
        self.decode = decode
        self.dispatch = dispatch
        self.raw_read = raw_read
        self.boundary = boundary
        self.messages = queue.Queue(maxsize=128)
        self.closed = threading.Event()
        self.eof = False
        self.session_id = None
        self.failure = None
        self.source = None
        self.session_closed = False
        self.air = None
        self.cleanup_deadline = None
        self.replaying = False
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    @staticmethod
    def initialize_meta():
        return {HEARTBEAT_KEY: {'v': 1, 'intervalMs': 5000, 'categories': CATEGORIES[:]}}

    @staticmethod
    def client_capabilities():
        return {'_meta': {'jetbrains': {'air': {'version': 1, 'capabilities': CAPABILITIES[:]}}}}

    def validate_support(self, result, quiet_timeout=60):
        meta = result.get('_meta')
        support = meta.get(HEARTBEAT_KEY) if isinstance(meta, dict) else None
        if (isinstance(support, dict) and type(support.get('v')) is int
                and support['v'] == 1 and isinstance(support.get('categories'), list)
                and all(category in support['categories'] for category in CATEGORIES)):
            return
        if supported(result):
            self.air = AirBackground(self, quiet_timeout)
            return
        raise ValueError('ACP wait background tasks requires Qwen active-work support or ACP AIR async-task support.')

    def begin_session(self, session_id, *, replay=False):
        self.session_id = session_id
        self.replaying = replay
        if self.air:
            self.air.reset()
            self.air.replaying = replay

    def end_replay(self):
        self.replaying = False
        if self.air:
            self.air.restored()

    def cleanup_request(self):
        if self.air:
            self.air.cleaning = True
            self.cleanup_deadline = time.monotonic() + CLEANUP_TIMEOUT_SECONDS
            return 'session/close', {'sessionId': self.session_id}
        return 'qwen/control/session/close', {
            'sessionId': self.session_id, 'requireFlush': True, 'drainTimeoutMs': 5000}

    def deadline_error(self, *, grace=0):
        if not self.air:
            return None
        deadline = self.cleanup_deadline if self.air.cleaning else self.air.deadline
        if deadline is not None and not self.air.cleaning:
            deadline += grace
        if deadline is not None and time.monotonic() >= deadline:
            return ('ACP background session cleanup timed out.' if self.air.cleaning else
                    'ACP AIR shutdown timed out.' if self.session_closed else
                    'ACP AIR background reconciliation timed out.')
        return None

    def confirm_cleanup(self, result):
        self.session_closed = (close_confirmed(result) if self.air else
                               result.get('sessionId') == self.session_id and result.get('closed') is True)

    def _read(self):
        try:
            while not self.closed.is_set() and not self.cancelled.is_set():
                line = self.stdout.readline()
                while not self.closed.is_set() and not self.cancelled.is_set():
                    try:
                        self.messages.put(line, timeout=0.1)
                        break
                    except queue.Full:
                        pass
                if not line:
                    return
        except Exception as error:
            while not self.closed.is_set() and not self.cancelled.is_set():
                try:
                    self.messages.put(error, timeout=0.1)
                    return
                except queue.Full:
                    pass

    def readline(self, size=-1, *, timeout=None):
        if self.eof:
            return b''
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.cancelled.is_set() and not self.closed.is_set():
            error = self.deadline_error()
            if error:
                raise ValueError(error)
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return None
            try:
                line = self.messages.get(timeout=0.1 if remaining is None else min(0.1, remaining))
            except queue.Empty:
                continue
            if isinstance(line, Exception):
                raise line
            if not line:
                self.eof = True
                return b''
            # Observe only parsed messages; the normal parser validates replies.
            self.observe(self.decode(line))
            return line
        raise ValueError('Remote agent cancelled')

    def observe(self, message):
        if self.air and self.session_id:
            self.air.observe(message)
            return
        if self.replaying:
            return
        params = message.get('params')
        if not isinstance(params, dict) or not self.session_id:
            return
        if params.get('sessionId') != self.session_id:
            return
        if message.get('method') == '_qwencode/end_turn':
            if params.get('reason') != 'end_turn':
                self.failure = 'ACP background continuation did not finish successfully.'
        if message.get('method') == 'session/update':
            update = params.get('update', {})
            meta = update.get('_meta') if isinstance(update, dict) else None
            source = meta.get('source') if isinstance(meta, dict) else None
            if source in ('background_notification', 'background_notification_response') and source != self.source:
                self.boundary()
                self.source = source

    def check(self):
        if self.cancelled.is_set() or self.requested.is_set():
            raise ValueError('Remote agent cancelled')
        if getattr(self, 'air', None):
            self.air.check()
            if self.air.failure:
                raise ValueError(self.air.failure)
        if self.failure:
            raise ValueError(self.failure)

    def pump(self, seconds, *, allow_eof=False):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.check()
            line = self.readline(timeout=min(0.1, max(0, deadline - time.monotonic())))
            if line is None:
                continue
            if not line:
                if allow_eof:
                    self.cancelled.wait(min(0.1, max(0, deadline - time.monotonic())))
                    continue
                raise ValueError('ACP stdout closed before background work was drained.')
            self.raw_read(line)
            message = self.decode(line)
            # No request is outstanding here. Replies cannot be silently lost.
            if 'id' in message and 'method' not in message:
                raise ValueError('Unexpected ACP response while waiting for background tasks.')
            self.dispatch(message)

    def wait(self, request, error_type, on_log):
        """Drain the negotiated backend; Qwen authorizes close atomically."""
        if getattr(self, 'air', None):
            return self.air.wait(request, on_log)
        reported_error = None
        while True:
            self.check()
            snapshot = request('qwen/status/session/tasks', {
                'sessionId': self.session_id, 'includeWorkflows': True})
            if (snapshot.get('sessionId') != self.session_id or type(snapshot.get('v')) is not int
                    or snapshot['v'] != 1 or not isinstance(snapshot.get('tasks'), list)):
                raise ValueError('Invalid ACP background task snapshot.')
            pending = False
            for task in snapshot['tasks']:
                if (not isinstance(task, dict) or not isinstance(task.get('status'), str)
                        or not task['status'] or not isinstance(task.get('id'), str) or not task['id']
                        or task.get('kind') not in ('shell', 'agent', 'monitor', 'workflow')):
                    raise ValueError('Invalid ACP background task status.')
                status = task['status']
                if status in ('failed', 'cancelled', 'canceled', 'aborted', 'interrupted'):
                    raise ValueError('ACP background task failed or was cancelled.')
                if status in ('completed', 'succeeded'):
                    code = task.get('exitCode')
                    if (code is not None and (type(code) is not int or code != 0)
                            or task.get('kind') == 'shell' and (type(code) is not int or code != 0)):
                        raise ValueError('ACP background shell did not exit successfully.')
                else:
                    # Paused, monitoring and unknown future active states keep
                    # the run alive under the caller's original deadline.
                    pending = True
            self.check()
            if not pending:
                try:
                    result = request('qwen/control/session/close', {
                        'sessionId': self.session_id, 'onlyIfUnheld': True,
                        'requireFlush': True, 'drainTimeoutMs': 5000})
                except error_type as error:
                    # A busy turn can exceed the drain budget. Never turn a
                    # failed close into permission to kill a successful run.
                    if str(error) != reported_error:
                        on_log(str(error))
                        reported_error = str(error)
                else:
                    self.check()
                    if result.get('sessionId') != self.session_id or type(result.get('closed')) is not bool:
                        raise ValueError('Invalid ACP conditional close response.')
                    if result['closed']:
                        self.session_closed = True
                        return
                    if not isinstance(result.get('holds'), list):
                        raise ValueError('Invalid ACP active-work holds.')
            self.pump(POLL_SECONDS)

    def close(self):
        self.closed.set()
        self.thread.join(0.2)

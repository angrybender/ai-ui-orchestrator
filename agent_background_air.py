"""Negotiated AIR task lifecycle and bounded quiet-period drain for ACP v1."""
import time


CAPABILITIES = ['asyncTasks', 'nativeSubagentSessions', 'sessionFailure']
FINAL_PROMPT = (
    'Background work has finished. Complete the original task: inspect the results '
    'of all background work, verify the outcome, and finish any remaining work. '
    'A failed background command does not by itself mean the original task failed: '
    'inspect its error, recover or retry as needed, and verify the requested result. '
    'Do not merely report that work was started. If you start more background '
    'work, its results must also be checked before the task is complete. '
    'Provide the final answer when the task is finished.'
)
LIMIT = 8192


def air_meta(value):
    if not isinstance(value, dict):
        return {}
    meta = value.get('_meta', {})
    jetbrains = meta.get('jetbrains', {}) if isinstance(meta, dict) else {}
    air = jetbrains.get('air', {}) if isinstance(jetbrains, dict) else {}
    return air if isinstance(air, dict) else {}


def supported(result):
    air = air_meta(result)
    caps = air.get('capabilities')
    agent = result.get('agentCapabilities', {})
    sessions = agent.get('sessionCapabilities', {}) if isinstance(agent, dict) else {}
    return (type(air.get('version')) is int and air['version'] >= 1
            and isinstance(caps, list) and all(c in caps for c in CAPABILITIES)
            and isinstance(sessions, dict) and isinstance(sessions.get('close'), dict))


def close_confirmed(result):
    return isinstance(result, dict) and not (set(result) - {'_meta'})


class AirBackground:
    def __init__(self, owner, quiet_timeout):
        if type(quiet_timeout) is not int or quiet_timeout < 1:
            raise ValueError('ACP background quiet timeout must be a positive integer.')
        self.owner = owner
        self.quiet_timeout = quiet_timeout
        self.reset()

    def reset(self):
        self.tasks = {}
        self.children = {}
        self.tools = {}
        self.unconfirmed_stops = {}
        self.work_version = 0
        self.last_activity = time.monotonic()
        self.replaying = False
        self.cleaning = False
        self.failure = None
        self.between_turns = False

    def restored(self):
        self.replaying = False
        # Historical failures do not fail a new turn, but restored live tasks
        # still require a result-processing turn.
        self.work_version = int(self.pending())
        self.last_activity = time.monotonic()

    def pending(self):
        return (any(state in ('running', 'paused') for state in self.tasks.values())
                or bool(self.unconfirmed_stops)
                or any(state == 'running' for _, state in self.children.values())
                or bool(self.tools))

    def check(self):
        # Claude can reconcile a disappeared task to stopped before its SDK
        # completion edge arrives. Await the correction, but never count an
        # unconfirmed stop as a successful result.
        if any(time.monotonic() - since >= self.quiet_timeout
               for since in self.unconfirmed_stops.values()):
            self.fail('ACP background task stop was not resolved to successful completion.')

    def fail(self, message):
        if not self.replaying and not self.cleaning:
            self.failure = message

    def result(self, result):
        if self.cleaning or self.replaying:
            return
        if 'stopReason' in result:
            self.between_turns = True
        failure = air_meta(result).get('sessionFailure')
        if failure is not None:
            if not isinstance(failure, dict) or failure.get('severity') not in ('warning', 'error'):
                self.fail('Invalid ACP AIR session failure.')
            elif failure['severity'] == 'error':
                self.fail('ACP AIR session reported an error.')

    @staticmethod
    def identifier(value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError('Invalid ACP AIR lifecycle identifier.')
        return value

    def observe(self, message):
        if self.cleaning:
            return
        if isinstance(message.get('result'), dict):
            self.result(message['result'])
            return
        params = message.get('params')
        if message.get('method') != 'session/update' or not isinstance(params, dict):
            return
        sid = params.get('sessionId')
        if sid != self.owner.session_id and sid not in self.children:
            return
        update = params.get('update')
        if not isinstance(update, dict):
            raise ValueError('Invalid ACP AIR update.')
        self.result(update)
        kind = update.get('sessionUpdate')
        if kind == 'agent_message_chunk' and sid == self.owner.session_id and self.between_turns:
            self.owner.boundary()
            self.between_turns = False
        if kind in ('async_task_spawned', 'async_task_state_update', 'async_task_progress'):
            key = (sid, self.identifier(update.get('asyncTaskId')))
            if kind == 'async_task_spawned':
                if self.tasks.get(key) != 'running':
                    self.unconfirmed_stops.pop(key, None)
                    self.tasks[key] = 'running'
                    self.work_version += 1
            elif key not in self.tasks:
                raise ValueError('ACP AIR task update without spawn.')
            elif kind == 'async_task_state_update':
                state = update.get('state')
                if state not in ('running', 'paused', 'completed', 'failed', 'stopped'):
                    raise ValueError('Invalid ACP AIR task state.')
                if self.tasks[key] != state:
                    self.work_version += 1
                self.tasks[key] = state
                if state == 'stopped' and not self.replaying:
                    self.unconfirmed_stops.setdefault(key, time.monotonic())
                else:
                    self.unconfirmed_stops.pop(key, None)
                # A command failure is input for the model's result-processing
                # turn, not a fatal failure of the ACP session. work_version
                # ensures that even an early/late failure gets finalized.
        elif kind in ('subagent_spawned', 'subagent_state_update'):
            child = self.identifier(update.get('subagentSessionId'))
            if kind == 'subagent_spawned':
                existing = self.children.get(child)
                if child in (self.owner.session_id, sid) or existing and existing[0] != sid:
                    raise ValueError('Invalid ACP AIR subagent tree.')
                if existing != (sid, 'running'):
                    self.children[child] = (sid, 'running')
                    self.work_version += 1
            else:
                if child not in self.children or self.children[child][0] != sid:
                    raise ValueError('ACP AIR subagent update without spawn.')
                state = update.get('state')
                if state not in ('running', 'completed', 'failed', 'cancelled', 'disconnected'):
                    raise ValueError('Invalid ACP AIR subagent state.')
                if self.children[child][1] != state:
                    self.work_version += 1
                self.children[child] = (sid, state)
                if state not in ('running', 'completed'):
                    self.fail('ACP background subagent failed or was disconnected.')
        elif kind in ('tool_call', 'tool_call_update'):
            key = (sid, self.identifier(update.get('toolCallId')))
            status = update.get('status', 'pending' if kind == 'tool_call' else None)
            if status in ('pending', 'in_progress'):
                self.tools[key] = status
            elif status in ('completed', 'failed'):
                self.tools.pop(key, None)
            elif status is not None:
                raise ValueError('Invalid ACP AIR tool status.')
        if len(self.tasks) + len(self.children) + len(self.tools) > LIMIT:
            raise ValueError('ACP AIR lifecycle registry limit exceeded.')
        # Usage, configuration and heartbeat traffic must not postpone shutdown.
        if (kind in ('agent_message_chunk', 'agent_thought_chunk', 'tool_call',
                     'tool_call_update', 'plan', 'state_update')
                or isinstance(kind, str) and kind.startswith(('async_task_', 'subagent_'))):
            self.last_activity = time.monotonic()
            if self.owner.session_closed:
                self.fail('ACP activity appeared after session close.')

    def wait(self, request, on_log):
        verified = 0
        self.last_activity = time.monotonic()
        on_log(f'Waiting for ACP AIR work and {self.quiet_timeout}s of inactivity.')
        while True:
            self.owner.check()
            # Consume queued late events before deciding whether work is done.
            self.owner.pump(0.01)
            if self.pending():
                self.owner.pump(0.1)
                continue
            if self.work_version != verified:
                version = self.work_version
                self.owner.boundary()
                result = request('session/prompt', {
                    'sessionId': self.owner.session_id,
                    'prompt': [{'type': 'text', 'text': FINAL_PROMPT}]})
                self.result(result)
                self.owner.check()
                if result.get('stopReason') != 'end_turn':
                    raise ValueError('ACP background finalization did not finish successfully.')
                verified = version
                self.last_activity = time.monotonic()
                continue
            remaining = self.quiet_timeout - (time.monotonic() - self.last_activity)
            if remaining > 0:
                self.owner.pump(min(0.1, remaining))
                continue
            self.owner.check()
            # Unlike Qwen this is a heuristic idle boundary, not atomic close.
            close_activity = self.last_activity
            result = request('session/close', {'sessionId': self.owner.session_id})
            self.owner.check()
            if not close_confirmed(result):
                raise ValueError('Invalid ACP session close response.')
            if (self.pending() or self.work_version != verified
                    or self.last_activity != close_activity):
                raise ValueError('ACP work appeared during session close.')
            self.owner.session_closed = True
            return

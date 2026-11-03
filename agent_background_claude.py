"""Explicit background drain negotiated with the pinned Claude adapter fork."""
import json
import time

from agent_background_air import AirBackground, FINAL_PROMPT, SHUTDOWN_TIMEOUT_SECONDS

CAPABILITY = 'orchestratorBackgroundTasks'
METHOD = '_orchestrator/background'
POLL_SECONDS = 1.0
RECONCILE_SECONDS = 30


def supported(result):
    meta = result.get('_meta', {})
    contract = meta.get(CAPABILITY) if isinstance(meta, dict) else None
    return isinstance(contract, dict) and type(contract.get('version')) is int and contract['version'] == 1


class ClaudeBackground(AirBackground):
    def check(self):
        # The adapter never synthesizes stopped from an absent list entry.
        # Only the original agent deadline limits background work and prompts.
        if self.cleaning:
            return
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise ValueError('ACP AIR shutdown timed out.')

    def observe(self, message):
        super().observe(message)
        # Negotiated fork emits stopped only for an actual SDK stop/TaskStop,
        # never as a guess based on absence from the background task list.
        self.unconfirmed_stops.clear()

    def snapshot(self, request, **extra):
        result = request(METHOD, {'sessionId': self.owner.session_id, **extra})
        if (not isinstance(result, dict) or type(result.get('version')) is not int
                or result['version'] != 1 or result.get('sessionId') != self.owner.session_id
                or type(result.get('pending')) is not bool
                or type(result.get('modelBusy')) is not bool
                or type(result.get('closed')) is not bool
                or not isinstance(result.get('token'), str) or not result['token']
                or not isinstance(result.get('tasks'), list) or len(result['tasks']) > 8192):
            raise ValueError('Invalid Claude background snapshot.')
        seen = set()
        for task in result['tasks']:
            if (not isinstance(task, dict) or not isinstance(task.get('id'), str) or not task['id']
                    or task['id'] in seen or type(task.get('autonomous')) is not bool
                    or task.get('state') not in ('running', 'paused', 'completed', 'failed', 'stopped')):
                raise ValueError('Invalid Claude background task.')
            seen.add(task['id'])
        if result['closed'] and (not extra.get('close') or result['pending'] or result['modelBusy']):
            raise ValueError('Invalid Claude conditional close response.')
        return result

    @staticmethod
    def work(snapshot):
        return json.dumps([(task['id'], task['state']) for task in snapshot['tasks']
                           if not task['autonomous']], separators=(',', ':'))

    def wait(self, request, on_log):
        verified = '[]'
        reconciled = None
        idle_since = time.monotonic()
        token = None
        on_log('Waiting for Claude native background tasks and automatic turns.')
        while True:
            self.owner.check()
            self.owner.pump(0.01)
            state = self.snapshot(request)
            self.owner.check()
            work = self.work(state)
            if state['token'] != token:
                token = state['token']
                idle_since = time.monotonic()
            # The same unchanged server gets one reconciliation prompt. A quiet
            # finite command is never an error or grounds for forced shutdown.
            reconcile = (state['pending'] and not state['modelBusy'] and work != reconciled
                         and time.monotonic() - idle_since >= RECONCILE_SECONDS)
            if (not state['pending'] and work != verified) or reconcile:
                on_log('Claude background result verification.' if not reconcile
                       else 'Claude background task reconciliation.')
                self.owner.boundary()
                result = request('session/prompt', {
                    'sessionId': self.owner.session_id,
                    'prompt': [{'type': 'text', 'text': FINAL_PROMPT + '\nKnown tasks: ' + work}]})
                self.result(result)
                self.owner.check()
                if result.get('stopReason') != 'end_turn':
                    raise ValueError('ACP background finalization did not finish successfully.')
                if reconcile:
                    reconciled = work
                else:
                    verified = work
                token = None
                idle_since = time.monotonic()
                continue
            if state['pending'] or self.pending():
                self.owner.pump(POLL_SECONDS)
                continue
            # Quiet is a stability check, never a failure deadline. The adapter
            # rechecks its own registry and generation before accepting close.
            if time.monotonic() - max(idle_since, self.last_activity) < self.quiet_timeout:
                self.owner.pump(POLL_SECONDS)
                continue
            close_activity = self.last_activity
            closed = self.snapshot(request, close=True, token=state['token'])
            self.owner.check()
            if not closed['closed']:
                token = None
                continue
            if self.pending() or self.last_activity != close_activity or self.work(closed) != work:
                raise ValueError('ACP work appeared during session close.')
            self.owner.session_closed = True
            self.deadline = time.monotonic() + SHUTDOWN_TIMEOUT_SECONDS
            return

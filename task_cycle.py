"""Persistent context ownership and run fencing, shared by Init and ACP."""
import json
import time

import board_store
from task_init import InitError


class TaskCycle:
    def __init__(self, run, config):
        self.run = run
        self.config = config
        self.identity = json.dumps([config.get('remote_server.host'), config.get('remote_server.username')])

    def active(self):
        with board_store.connect() as db:
            return bool(db.execute("SELECT 1 FROM agent_runs r JOIN board_tasks t ON t.id = r.task_pk "
                                   "WHERE r.id = ? AND t.active_run_id = r.id AND t.status = 'IN PROGRESS' "
                                   "AND r.state IN ('PREPARING', 'RUNNING')", (self.run['id'],)).fetchone())

    def check(self):
        if not self.active():
            raise InitError('Init script error: cancelled')

    def phase(self, value):
        timeout = (self.config.get('tasks.init_script_timeout') or 300) if value == 'Init' else self.config['agent.agent_timeout']
        with board_store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            changed = db.execute("UPDATE board_tasks SET phase = ? WHERE active_run_id = ? AND status = 'IN PROGRESS'", (value, self.run['id']))
            if changed.rowcount != 1:
                raise InitError('Init script error: cancelled')
            db.execute("UPDATE agent_runs SET phase = ?, started_at = ?, timeout = ? WHERE id = ?", (value, time.time(), timeout, self.run['id']))

    def prepared(self, cwd):
        with board_store.connect() as db:
            db.execute("UPDATE board_tasks SET remote_cwd = ?, remote_identity = ?, context_ready = 1 WHERE active_run_id = ?",
                       (cwd, self.identity, self.run['id']))
        self.check()

    def reuse(self, cwd):
        if self.run.get('context_ready'):
            if self.run.get('remote_cwd') != cwd or self.run.get('remote_identity') != self.identity:
                raise InitError('Init script error: saved task directory does not match remote configuration')
            return True
        # Existing installations already have ACP sessions; preserve their resume.
        return bool(self.run.get('session_id'))

    def pid(self, pid):
        with board_store.connect() as db:
            db.execute('UPDATE agent_runs SET init_pid = ? WHERE id = ?', (pid, self.run['id']))
        self.check()

    def agent_pid(self, pid):
        with board_store.connect() as db:
            db.execute('UPDATE agent_runs SET agent_pid = ? WHERE id = ?', (pid, self.run['id']))
        self.check()

    def cancelling(self):
        with board_store.connect() as db:
            db.execute('UPDATE agent_runs SET cancel_requested_at = COALESCE(cancel_requested_at, ?) WHERE id = ?',
                       (time.time(), self.run['id']))

    def agent_stopped(self, confirmed):
        with board_store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE agent_runs SET agent_stop_confirmed = ?, agent_uncertain = ? WHERE id = ?',
                       (confirmed, not confirmed, self.run['id']))
            if not confirmed:
                db.execute('UPDATE board_tasks SET agent_uncertain = 1 WHERE id = '
                           '(SELECT task_pk FROM agent_runs WHERE id = ?)', (self.run['id'],))
            else:
                # Recovery may have marked the task uncertain while cleanup
                # was completing. Keep task and run flags consistent without
                # clearing uncertainty belonging to another run of this task.
                db.execute('UPDATE board_tasks SET agent_uncertain = 0 WHERE id = '
                           '(SELECT task_pk FROM agent_runs WHERE id = ?) AND NOT EXISTS '
                           '(SELECT 1 FROM agent_runs WHERE task_pk = board_tasks.id AND agent_uncertain = 1)',
                           (self.run['id'],))

    def initialized(self):
        with board_store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("UPDATE agent_runs SET init_result = 'SUCCEEDED' WHERE id = ? AND state IN ('PREPARING', 'RUNNING')", (self.run['id'],))
            db.execute('UPDATE board_tasks SET init_succeeded = 1 WHERE active_run_id = ?', (self.run['id'],))
        self.check()

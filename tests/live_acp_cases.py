"""Model/tool/lifecycle cases ACP-13..26 for the opt-in live matrix."""
from contextlib import suppress
import json
import shlex
import time

from live_acp_client import BACKGROUND, RpcError, poll


def native_tasks(state):
    return [task for task in state['tasks'] if not task.get('autonomous')]


def reply_text(client):
    chunks = []
    for event in client.events:
        params = event.get('params', {})
        update = params.get('update', {})
        if params.get('sessionId') == client.session and update.get('sessionUpdate') == 'agent_message_chunk':
            chunks.append(update.get('content', {}).get('text', ''))
    return ''.join(chunks)


def background_prompt(agent, commands):
    tool = ('Use native Bash calls with run_in_background=true.' if agent == 'claude' else
            'Use native exec_command calls with yield_time_ms=1000 so each returns a live session_id.')
    return ('Isolated background lifecycle test. '+tool+' Run each command below as its own native background task in the current directory. '
            'Do not use &, nohup, subprocess.Popen, setsid, a detached wrapper, or subagents. '
            'Do not poll or wait for completion and do not stop these commands. '
            'Immediately after launch, reply STARTED and finish this turn while commands are still running.\n'
            + '\n'.join('```sh\n'+command+'\n```' for command in commands))


def start_background(client, commands):
    result = client.prompt(background_prompt(client.agent, commands), 150)
    assert result['stopReason'] == 'end_turn', result
    return wait_snapshot(client, lambda state: bool(native_tasks(state)), 30)


def wait_snapshot(client, predicate, timeout=90):
    def matched():
        state = client.snapshot()
        return state if predicate(state) else None
    return poll(matched, timeout)


def idle(client):
    return wait_snapshot(client, lambda state: not state['pending'] and not state['modelBusy'], 150)


def verified(client, files):
    result = client.prompt('Read the following files using native tools in the current directory. Verify their exact contents: '
                           + json.dumps(files)+'. Do not launch new work or subagents. Reply VERIFIED_RESULTS after actually reading them.', 150)
    assert result['stopReason'] == 'end_turn', result
    assert 'VERIFIED_RESULTS' in reply_text(client), reply_text(client)[-600:]
    for file, expected in files.items():
        actual = client.env.read(client.cwd+'/'+file)
        assert actual == expected, (file, actual)


def stop_task(client, task_id, session=None):
    return client.request('_session/async_task/stop', {'sessionId': session or client.session, 'asyncTaskId': task_id}, 30)


def close_cleanly(client):
    state = idle(client)
    assert client.snapshot(close=True, token=state['token'])['closed']
    client.close()
    assert not client.forced_stop, 'ACP did not exit after conditional close'
    poll(lambda: not client.env.processes(client.cwd), 5)


def advance(env, agent, case):
    client = env.client(agent, case, invalid=case == 'ACP-26')
    client.new_session()
    if case == 'ACP-13':
        state = start_background(client, ['sleep 18; printf FINITE_OK > finite.txt'])
        assert state['pending'], state
        assert not client.snapshot(close=True, token=state['token'])['closed']
        poll(lambda: env.read(client.cwd+'/finite.txt') == 'FINITE_OK', 90)
        done = idle(client)
        assert any(task['state'] == 'completed' for task in native_tasks(done)), done
        assert not client.snapshot(close=True, token=state['token'])['closed']
        verified(client, {'finite.txt': 'FINITE_OK'})
        close_cleanly(client)
        return 'Finite native task completed; stale close refused; result verified; processes absent'
    if case == 'ACP-14':
        for number in range(1, 5):
            start_background(client, [f'sleep 6; printf GENERATION_{number} > generation-{number}.txt'])
            poll(lambda: env.read(client.cwd+f'/generation-{number}.txt') == f'GENERATION_{number}', 90)
            idle(client)
        state = client.snapshot()
        assert len(native_tasks(state)) >= 4, state
        verified(client, {f'generation-{number}.txt': f'GENERATION_{number}' for number in range(1, 5)})
        close_cleanly(client)
        return 'Four native generations completed and independently verified'
    if case == 'ACP-15':
        start_background(client, ['sleep 14; printf PARALLEL_A > a.txt', 'sleep 18; printf PARALLEL_B > b.txt'])
        state = client.snapshot()
        assert len(native_tasks(state)) >= 2, state
        for file, value in {'a.txt': 'PARALLEL_A', 'b.txt': 'PARALLEL_B'}.items():
            poll(lambda: env.read(client.cwd+'/'+file) == value, 90)
        verified(client, {'a.txt': 'PARALLEL_A', 'b.txt': 'PARALLEL_B'})
        close_cleanly(client)
        return 'Two parallel native tasks completed and verified'
    if case == 'ACP-16':
        start_background(client, ['sleep 6; printf EXPECTED_ERROR >&2; exit 7'])
        state = wait_snapshot(client, lambda state: any(task['state'] == 'failed' for task in native_tasks(state)), 90)
        client.prompt('The exit-7 command was deliberately unsuccessful. Inspect its existing task output, acknowledge EXPECTED_ERROR, and finish. Do not retry or launch new work.')
        assert 'EXPECTED_ERROR' in reply_text(client)
        close_cleanly(client)
        return 'Exit 7 retained as failed and handled without mandatory retry'
    if case in ('ACP-17', 'ACP-18'):
        delay = 60 if case == 'ACP-18' else 40
        state = start_background(client, [f'printf STARTED > started.txt; sleep {delay}; printf AFTER_STOP > stopped.txt'])
        task = next(task for task in native_tasks(state) if task['state'] == 'running')
        task_id = task['id']
        assert stop_task(client, task['id']).get('stopped') is True
        assert stop_task(client, task['id']).get('stopped') is False
        assert stop_task(client, 'unknown-task-id').get('stopped') is False
        if case == 'ACP-18':
            deadline = time.monotonic()+65
            while time.monotonic() < deadline:
                assert env.read(client.cwd+'/stopped.txt') is None
                time.sleep(.5)
        assert env.read(client.cwd+'/stopped.txt') is None
        state = client.snapshot()
        assert next(row for row in native_tasks(state) if row['id'] == task_id)['state'] == 'stopped'
        close_cleanly(client)
        return 'Native stop confirmed; no late write'+(' after 65 seconds' if case == 'ACP-18' else '')
    if case == 'ACP-19':
        request_id = client.begin_prompt('Use a foreground native Bash/shell tool to run `echo $$ > cancel.pid; sleep 60; printf AFTER_CANCEL > cancel.txt`. Wait in the tool; do not background or detach it.')
        poll(lambda: env.read(client.cwd+'/cancel.pid'), 90)
        client.send({'jsonrpc': '2.0', 'method': 'session/cancel', 'params': {'sessionId': client.session}})
        result = client.wait(request_id, 30)
        assert result['stopReason'] == 'cancelled', result
        client.close()
        assert env.read(client.cwd+'/cancel.txt') is None
        assert not env.processes(client.cwd), env.processes(client.cwd)
        return 'Cancellation after actual command startup; no surviving process or output'
    if case == 'ACP-20':
        code = 'import http.server,socketserver,pathlib; s=socketserver.TCPServer(("127.0.0.1",0),http.server.SimpleHTTPRequestHandler); pathlib.Path("port.txt").write_text(str(s.server_address[1])); s.serve_forever()'
        state = start_background(client, ['python3 -c '+shlex.quote(code)])
        port = int(poll(lambda: env.read(client.cwd+'/port.txt'), 60))
        status = env.command('python3 -c '+shlex.quote(f'import urllib.request; print(urllib.request.urlopen("http://127.0.0.1:{port}", timeout=3).status)'))
        assert status.strip() == '200', status
        assert state['pending'] and not client.snapshot(close=True, token=state['token'])['closed']
        task = next(task for task in native_tasks(state) if task['state'] == 'running')
        assert stop_task(client, task['id']).get('stopped') is True
        close_cleanly(client)
        return 'Loopback HTTP 200, busy close refusal, native stop and complete process cleanup'
    if case in ('ACP-21', 'ACP-22', 'ACP-23'):
        if case == 'ACP-21':
            prompt = ('Create exactly one native subagent. Have it run `printf CHILD_OK > child.txt` in '+client.cwd+
                      '. Wait for its completion, read child.txt yourself, and reply CHILD_VERIFIED. Do not do the child work yourself.')
        else:
            delay = 60 if case == 'ACP-23' else 18
            child_instruction = background_prompt(agent, [f'sleep {delay}; printf CHILD_BG_OK > child-bg.txt'])
            prompt = ('Create exactly one native subagent with cwd '+client.cwd+'. Give it this task: '+child_instruction+
                      '\nWhen the child returns after launching work, immediately reply CHILD_STARTED yourself. Do not stop its necessary finite background work.')
            if case == 'ACP-23':
                prompt += ' The client will test cancellation. Do not retry or restart any stopped command; the client performs all subsequent checks.'
        result = client.prompt(prompt, 180)
        assert result['stopReason'] == 'end_turn', result
        children = [event.get('params', {}).get('update', {}) for event in client.events
                    if event.get('params', {}).get('update', {}).get('sessionUpdate') == 'subagent_spawned']
        assert children, 'No native subagent lifecycle was observed'
        if case == 'ACP-21':
            assert env.read(client.cwd+'/child.txt') == 'CHILD_OK'
            assert 'CHILD_VERIFIED' in reply_text(client)
        elif case == 'ACP-22':
            def child_result():
                if env.read(client.cwd+'/child-bg.txt') == 'CHILD_BG_OK':
                    return 'complete'
                state = client.snapshot()
                if native_tasks(state) and not state['pending'] and any(task['state'] == 'stopped' for task in native_tasks(state)):
                    return 'stopped'
                return None
            if poll(child_result, 90) == 'stopped':
                return {'status': 'partial', 'detail': 'SDK stopped finite child work at handback; file was not produced. Persistence is not guaranteed.'}
            verified(client, {'child-bg.txt': 'CHILD_BG_OK'})
        else:
            child = children[-1]['subagentSessionId']
            tasks = [event['params']['update']['asyncTaskId'] for event in client.events
                     if event.get('params', {}).get('sessionId') == child and event.get('params', {}).get('update', {}).get('sessionUpdate') == 'async_task_spawned']
            assert tasks or agent == 'claude', 'Child did not launch an observable native task'
            # The root snapshot owns native tasks across the child tree. Its
            # public IDs are stopped through the root session, not the child.
            running = [task for task in native_tasks(client.snapshot()) if task['state'] == 'running']
            if running:
                assert len(running) == 1, running
                assert stop_task(client, running[0]['id']).get('stopped') is True
            else:
                assert agent == 'claude' and any(task['state'] == 'stopped' for task in native_tasks(client.snapshot()))
            deadline = time.monotonic()+65
            while time.monotonic() < deadline:
                assert env.read(client.cwd+'/child-bg.txt') is None
                time.sleep(.5)
        close_cleanly(client)
        return 'Native child lifecycle and independent filesystem/process check passed'
    if case == 'ACP-24':
        client.prompt('Use native shell to create resume.txt containing exactly RESUME_OK. Read it and finish.')
        assert env.read(client.cwd+'/resume.txt') == 'RESUME_OK'
        session = client.session
        state = idle(client)
        assert client.snapshot(close=True, token=state['token'])['closed']
        client.close()
        resumed = env.client(agent, case)
        resumed.request('session/load', {'sessionId': session, 'cwd': resumed.cwd, 'mcpServers': []})
        resumed.session = session
        resumed.prompt('Read resume.txt using native tools. Verify RESUME_OK and reply RESUME_VERIFIED.')
        assert 'RESUME_VERIFIED' in reply_text(resumed)
        close_cleanly(resumed)
        return 'Persisted session loaded and existing file verified'
    if case == 'ACP-25':
        state = start_background(client, ['sleep 60; printf LOST_SDK > lost.txt'])
        assert state['pending']
        processes = env.processes(client.cwd)
        sdk = next((row for row in processes if row['command'].startswith(('codex', 'claude'))), None)
        assert sdk, {'no_sdk_process': processes}
        env.command('kill -TERM '+str(sdk['pid']))
        try:
            state = client.snapshot()
            assert state['pending'] and not state['closed'], state
            assert not client.snapshot(close=True, token=state['token'])['closed']
        except (RpcError, RuntimeError):
            pass
        return 'Lost SDK cannot authorize successful close; residual processes cleaned independently'
    if case == 'ACP-26':
        try:
            result = client.prompt('Reply AUTH_TEST without tools.', 25)
        except RpcError as error:
            return 'Authentication error delivered: '+str(error)
        failure = result.get('_meta', {}).get('jetbrains', {}).get('air', {}).get('sessionFailure', {})
        assert failure.get('severity') == 'error', result
        return 'Authentication failure explicitly reported through AIR'
    raise AssertionError('Unimplemented case '+case)

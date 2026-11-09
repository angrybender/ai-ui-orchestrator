"""Explicit, paid live ACP matrix. Never collected by the offline pytest suite.

python tests/live_acp_matrix.py --key-file /private/key --endpoint https://routerai.ru/api/v1
"""
from __future__ import annotations

import argparse
from contextlib import suppress
import json
from pathlib import Path
import signal
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from live_acp_client import BACKGROUND, CAPABILITIES, Environment, RpcError, poll
from live_acp_cases import advance
from live_acp_integration import integration


def rejected(callback):
    try:
        callback()
    except RpcError as error:
        return error.code
    raise AssertionError('Invalid request was accepted')


def idle(client):
    return poll(lambda: (state if not state['pending'] and not state['modelBusy'] else None)
                if (state := client.snapshot()) else None, 30)


def text_prompt(command, marker='VERIFIED_OK'):
    return ('This is an isolated integration test. Use your native shell/Bash tool to run exactly this command in the current directory:\n```sh\n'
            + command + '\n```\nRead the result file with a tool and verify its exact contents. Do not create extra files or commands. '
            'Do not use subagents. Reply '+marker+' only after checking the file.')


def protocol(env, agent, case):
    caps = {} if case == 'ACP-02' else ({'_meta': {'orchestratorBackgroundTasks': 1}} if case == 'ACP-03' else CAPABILITIES)
    client = env.client(agent, case, capabilities=caps)
    result = client.initialized
    if case == 'ACP-01':
        assert result['protocolVersion'] == 1
        assert result.get('_meta', {}).get('orchestratorBackgroundTasks', {}).get('version') == 1, result
        return 'ACP v1 and background v1 negotiated'
    if case == 'ACP-02':
        assert 'orchestratorBackgroundTasks' not in result.get('_meta', {})
        rejected(lambda: client.request(BACKGROUND, {'sessionId': 'missing'}))
        return 'Unnegotiated background RPC rejected'
    if case == 'ACP-03':
        from agent_background_air import supported
        assert not supported(result)
        if agent == 'codex':
            assert 'orchestratorBackgroundTasks' not in result.get('_meta', {})
        return 'Incomplete AIR cannot enable orchestrator drain'
    if case == 'ACP-04':
        for params in ({}, {'sessionId': 'missing', 'close': 'yes'}, {'sessionId': 'missing', 'token': 7}):
            rejected(lambda params=params: client.request(BACKGROUND, params))
        return 'Malformed background requests rejected'
    client.new_session()
    if case == 'ACP-05':
        if agent == 'codex':
            rejected(lambda: client.snapshot(unexpected=True))
            return 'Unknown field rejected'
        assert client.snapshot(unexpected=True)['closed'] is False
        return 'Documented Claude parser behavior: unknown field accepted'
    if case == 'ACP-06':
        rejected(lambda: client.request(BACKGROUND, {'sessionId': 'missing'}))
        state = idle(client)
        assert client.snapshot(close=True, token=state['token'])['closed']
        rejected(lambda: client.snapshot())
        return 'Unknown and closed sessions rejected'
    if case == 'ACP-07':
        state = idle(client)
        assert client.snapshot()['token'] == state['token']
        return 'Idle snapshot token stable'
    if case == 'ACP-08':
        assert not client.snapshot(close=True)['closed']
        assert not client.snapshot(close=True, token='stale-token')['closed']
        return 'Missing and stale tokens cannot close'
    if case == 'ACP-09':
        state = idle(client)
        ids = [client.start(BACKGROUND, {'sessionId': client.session, 'close': True, 'token': state['token']}) for _ in range(2)]
        closed = 0
        for request_id in ids:
            try:
                closed += client.wait(request_id).get('closed') is True
            except RpcError:
                pass
        assert closed == 1
        return 'Exactly one concurrent close accepted'
    if case == 'ACP-10':
        state = idle(client)
        close_id = client.start(BACKGROUND, {'sessionId': client.session, 'close': True, 'token': state['token']})
        prompt_id = client.begin_prompt(text_prompt('printf RACE_OK > race.txt'))
        closed = client.wait(close_id)['closed']
        if closed:
            rejected(lambda: client.wait(prompt_id))
            assert env.read(client.cwd+'/race.txt') is None
        else:
            assert client.wait(prompt_id, 150)['stopReason'] == 'end_turn'
            assert env.read(client.cwd+'/race.txt') == 'RACE_OK'
        return 'Prompt/close interleaving preserved session exclusion'
    if case == 'ACP-11':
        request_id = client.begin_prompt(text_prompt('printf STARTED > started.txt; sleep 10; printf BUSY_OK > busy.txt'))
        poll(lambda: env.read(client.cwd+'/started.txt'), 90)
        state = client.snapshot()
        assert state['pending'] or state['modelBusy'], state
        assert not client.snapshot(close=True, token=state['token'])['closed']
        assert client.wait(request_id, 150)['stopReason'] == 'end_turn'
        assert env.read(client.cwd+'/busy.txt') == 'BUSY_OK'
        return 'Busy close refused; command completed'
    if case == 'ACP-12':
        result = client.prompt(text_prompt('printf FOREGROUND_OK > result.txt'))
        assert result['stopReason'] == 'end_turn', result
        actual = env.read(client.cwd+'/result.txt')
        assert actual == 'FOREGROUND_OK', {'file_contents': actual, 'result': result}
        return 'Native shell result independently verified'
    raise AssertionError('Unimplemented case '+case)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--key-file', required=True, type=Path)
    parser.add_argument('--endpoint', required=True)
    parser.add_argument('--agents', nargs='+', choices=('codex', 'claude'), default=['codex', 'claude'])
    parser.add_argument('--cases', default=','.join(['ACP-%02d' % number for number in range(1, 27)]+['INT-%02d' % number for number in range(1, 8)]))
    parser.add_argument('--report', type=Path)
    parser.add_argument('--codex-binary', help='Isolated installed runtime to verify a Codex adapter fix')
    args = parser.parse_args()
    valid = {'ACP-%02d' % number for number in range(1, 27)} | {'INT-%02d' % number for number in range(1, 8)}
    if any(case not in valid for case in args.cases.split(',')):
        parser.error('Cases must be ACP-01..26 or INT-01..07')
    def interrupted(*_):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    key = args.key_file.read_text().strip()
    env = Environment(key, args.endpoint, codex_binary=args.codex_binary)
    results = []
    print(json.dumps({'event': 'started', 'directory': env.root, 'endpoint': env.endpoint}), flush=True)
    try:
        for agent in args.agents:
            for case in args.cases.split(','):
                started = time.monotonic()
                try:
                    if case.startswith('INT-'):
                        detail = integration(env, agent, case)
                    else:
                        detail = protocol(env, agent, case) if int(case.split('-')[1]) <= 12 else advance(env, agent, case)
                    result = {'agent': agent, 'case': case, 'status': 'passed', 'detail': detail}
                    if isinstance(detail, dict) and detail.get('status') == 'partial':
                        result['status'] = 'partial'
                    if isinstance(detail, dict) and detail.get('residual_processes', 0):
                        result['status'] = 'partial'
                except Exception as error:
                    result = {'agent': agent, 'case': case, 'status': 'failed', 'detail': env.safe(type(error).__name__+': '+str(error))[:2000]}
                    if env.clients:
                        client = env.clients[-1]
                        result['diagnostic'] = env.safe(json.dumps(client.events[-4:], ensure_ascii=False))[-3000:]
                        result['stderr'] = env.safe(''.join(client.errors))[-1000:]
                        with suppress(Exception):
                            result['snapshot'] = client.snapshot()
                finally:
                    for client in env.clients:
                        if not client.channel.closed:
                            with suppress(Exception):
                                client.close()
                    with suppress(Exception):
                        env.processes(stop=True)
                result['seconds'] = round(time.monotonic()-started, 2)
                results.append(result)
                print(json.dumps(result, ensure_ascii=False), flush=True)
                if args.report:
                    args.report.write_text(json.dumps({'endpoint': env.endpoint, 'remote_root': env.root, 'results': results}, ensure_ascii=False, indent=2)+'\n')
    finally:
        env.cleanup()
    return int(any(result['status'] != 'passed' for result in results))


if __name__ == '__main__':
    raise SystemExit(main())

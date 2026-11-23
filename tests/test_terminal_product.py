from contextlib import nullcontext
import io
import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from autotuner import cli, terminal
from autotuner.artifacts import read_json, write_new
from autotuner.comparison import compare
from autotuner.monitor import Monitor, run
from autotuner.terminal_ui import Keyboard, Theme, clip


def snapshot():
    stats = dict(count=2, requests_per_second=.1, p95_ms=3.3, error_rate=0., cache_hits=1)
    return dict(schema_version=1, status=dict(effective_mode='adaptive', session_id='current-session',
        cache=dict(entries=1, accounted_bytes=1, byte_budget=500), storage=dict(state='ready'),
        limits={}, runtime=dict(in_flight=0), transport={}),
        metrics=dict(summary=stats, routes={'GET /items':stats}, window_may_be_incomplete=False),
        findings=[dict(operation='GET /items',code='slow_upstream',recommendation='Inspect origin')],
        policies=[dict(id='items',match='/items',state='serving',detail='slow reused origin')],
        decisions=[dict(rule_id='items',action='enabled',reason='slow_reused_origin',new_ttl_seconds=2.)])


def matched(stage):
    fingerprint = 'a'*64
    duration = 10 if stage=='baseline' else 2
    return dict(schema_version=1, kind='active', stage=stage, state='completed', session_id=stage, baseline_id=stage,
        comparison_context=fingerprint, client_source_sha256=fingerprint, proxy_source_sha256=fingerprint,
        workload_digest=fingerprint, workload=dict(method='GET', requests=2, warmup=1, concurrency=1,
            expected_status=200, app_state_digest=fingerprint),
        active_result=dict(state='completed', elapsed_seconds=.4, completed=2, errors=0, warmup_completed=1,
            samples=[dict(status=200, duration_ms=duration, passed=True, response_sha256=fingerprint,
                          cache='BYPASS' if stage=='baseline' else 'HIT') for _ in range(2)]))


def test_compare_known_savings_and_atomic_output(tmp_path, capsys):
    before, after, out = [tmp_path/name for name in ('before.json','after.json','comparison.json')]
    write_new(before, matched('baseline'))
    write_new(after, matched('optimized'))
    command = ['compare',str(before),str(after),'--output',str(out),'--json']
    assert cli.main(command) == 0
    result, raw = read_json(out, 16384)
    assert json.loads(capsys.readouterr().out) == result
    assert result['latency_reduction_percent']['mean_ms'] == 80
    assert result['baseline']['hits'] == 0 and result['optimized']['hits'] == 2
    assert cli.main(command) == 2
    assert out.read_bytes() == raw


@pytest.mark.parametrize('field,value,reason', [
    ('state','incomplete','optimized_missing_completed_matched_workload'),
    ('workload_digest','b'*64,'different_or_missing_workload_digest'),
    ('session_id','baseline','distinct_proxy_sessions_required'),
    ('proxy_source_sha256',None,'different_or_missing_proxy_source_sha256'),
])
def test_incomparable_captures(field, value, reason):
    after = matched('optimized')
    after[field] = value
    result = compare(matched('baseline'), after)
    assert not result['comparable'] and reason in result['reasons']
    assert 'latency_reduction_percent' not in result


def test_comparison_rejects_changed_response():
    after = matched('optimized')
    after['active_result']['samples'][0]['response_sha256'] = 'b'*64
    assert compare(matched('baseline'), after)['reasons'] == ['response_content_changed']


def test_export_failure_keeps_existing_file_and_leaves_no_partial(tmp_path):
    target = tmp_path/'out.json'
    with pytest.raises(ValueError):
        write_new(target, {'large':'x'*1000}, limit=20)
    assert not list(tmp_path.iterdir())
    with pytest.raises(ValueError):
        write_new(target, {'nan':float('nan')})
    assert not list(tmp_path.iterdir())


def test_monitor_live_state_actions_and_resize():
    data = snapshot()
    clock = [10]
    out = io.StringIO()
    fetch = Mock(side_effect=[data, OSError('private'), data])
    monitor = Monitor(fetch, (OSError,), Theme(out), 60, 2, '', clock=lambda:clock[0])
    monitor.update()
    assert 'GET /items' in '\n'.join(monitor.lines(100, 27))
    monitor.input('right')
    assert 'slow_reused_origin' in '\n'.join(monitor.lines(80, 18))
    monitor.input(' ')
    clock[0] = 17
    assert 'PAUSED / STALE' in '\n'.join(monitor.lines(80, 18))
    monitor.input('r')
    assert monitor.error and monitor.data is data
    assert 'DISCONNECTED / LAST SNAPSHOT' in '\n'.join(monitor.lines(80, 18))
    monitor.input('r')
    monitor.input('3')
    assert not monitor.error and 'Slow upstream' in '\n'.join(monitor.lines(68, 18))
    monitor.input('i')
    monitor.input('4')
    assert monitor.interval == 5 and 'runtime' in '\n'.join(monitor.lines(80, 18))
    lines = monitor.lines(25, 5)
    assert len(lines) <= 5 and all(len(line) <= 25 for line in lines)
    assert clip('visible\x1b[31m🔍', 10) == 'visible [3'


def test_monitor_once_export_and_json(tmp_path):
    output = tmp_path/'snapshot.json'
    stream = io.StringIO()
    args = SimpleNamespace(output=output, json=True, once=False, window=60, filter='')
    assert run(lambda _:snapshot(), args, (OSError,), stdin=io.StringIO(), stdout=stream) == 0
    assert json.loads(stream.getvalue()) == json.loads(output.read_text()) == snapshot()


def test_theme_pixel_banner_and_ascii_fallback():
    class ASCII(io.StringIO):
        encoding = 'ascii'
    palette = Theme(ASCII())
    palette.rich = True
    lines = palette.banner(75, 25)
    assert len(lines) == 5 and all('#' in line and '\x1b[38;2;' in line for line in lines)
    assert 'AUTOTUNER' in palette.banner(30, 12)[0]


def test_menu_guided_setup_and_policy_revision(tmp_path):
    (tmp_path/'openapi.json').write_text('{"openapi":"3.0.0","paths":{"/items":{"get":{}}}}')
    class TTY(io.StringIO):
        def isatty(self):
            return True
    out = TTY()
    # Discover and select route 1, then approve caching with a PATCH invalidation.
    keys = '1\n7\n\n1\n\nbase.json\n1\nb\n3\n4\n/items\n3\n/items/*\n5\n\nactive.json\n1\nq\n'
    assert terminal.run(project=tmp_path, invoke=cli.main, stdin=TTY(keys), stdout=out) == 0
    assert (tmp_path/'base.json').exists() and (tmp_path/'active.json').exists()
    rule, = json.loads((tmp_path/'active.json').read_text())['config']['cache']['routes']
    assert rule['invalidate_on'] == [{'method':'PATCH', 'path':'/items/*'}]
    assert 'for the next proxy start' in out.getvalue()


def test_rich_menu_arrows_render_selection_and_return(tmp_path, monkeypatch):
    stream = io.StringIO()
    workspace = terminal.Workspace(tmp_path, tmp_path/'base.json', Mock(), io.StringIO(), stream)
    workspace.theme.rich = True
    workspace.keyboard = SimpleNamespace(active=nullcontext, read=Mock(side_effect=['down','down','up','enter','back']))
    monkeypatch.setattr(terminal, 'terminal_size', lambda:(80,25))
    assert workspace.choose(None) == '2'
    assert workspace.choose('2') == 'b'
    assert '\x1b[H' in stream.getvalue() and '[1;48;2;79;46;131' in stream.getvalue()


def test_idle_menu_draws_once_and_restores_screen_on_interrupt(tmp_path, monkeypatch):
    stream = io.StringIO()
    workspace = terminal.Workspace(tmp_path, tmp_path/'base.json', Mock(), io.StringIO(), stream)
    workspace.theme.rich = True
    workspace.keyboard = SimpleNamespace(active=nullcontext, read=Mock(side_effect=[None, '', None, KeyboardInterrupt]))
    monkeypatch.setattr(terminal, 'terminal_size', lambda: (75, 16))
    with pytest.raises(KeyboardInterrupt):
        workspace.choose(None)
    output = stream.getvalue()
    assert output.count('\x1b[H') == 1
    assert output.startswith('\x1b[?1049h') and output.endswith('\x1b[?1049l')
    assert not output.removesuffix('\x1b[?1049l').endswith('\n')


def test_resource_units_and_wrapped_findings():
    from autotuner.monitor import tab_rows
    data = snapshot()
    data['status']['storage'].update(disk_bytes=4096, last_error=None, persistent=True,
                                     transport={'completed_requests': 25})
    data['status']['transport'].update(queue_ms=2.6847998, upstream_requests=25)
    resources = '\n'.join(tab_rows(data, 3, ''))
    assert '4.0 KiB' in resources and '2.68 ms' in resources and 'last error   none' in resources
    assert 'persistent   yes' in resources and 'storage / transport' not in resources
    assert tab_rows(data, 3, 'queue') == ['transport / queue   2.68 ms']
    data['findings'][0]['recommendation'] = 'Check public response eligibility and invalidation on write.'
    rows = tab_rows(data, 2, '', width=30)
    assert len(rows) > 2 and all(len(row) <= 30 for row in rows)
    assert rows[-1].endswith('on write.')
    rows = tab_rows(data, 0, '', width=40)
    assert rows[0] == 'GET /items' and rows[-1].endswith('1 hit')


def test_monitor_key_loop_with_refresh_filter_resize_and_exit(monkeypatch):
    data = snapshot()
    data['metrics']['window_may_be_incomplete'] = True
    data['status']['storage']['state'] = 'degraded'
    fetch = Mock(return_value=data)
    output = io.StringIO()
    theme = Theme(output)
    theme.rich = True
    keyboard = SimpleNamespace(active=nullcontext, read=Mock(side_effect=['/', 'j',' ','r','q']))
    view = Monitor(fetch, (OSError,), theme, 60, 2, '', clock=lambda:10)
    view.run(io.StringIO('items\n'), output, keyboard)
    assert fetch.call_count == 2 and view.paused and view.pattern=='items'
    assert 'PARTIAL WINDOW' in output.getvalue() and 'STORAGE DEGRADED' in output.getvalue()


def test_plain_monitor_preserves_last_snapshot_and_returns_at_eof():
    output = io.StringIO()
    view = Monitor(Mock(side_effect=[OSError('test only'),snapshot(),snapshot()]),(OSError,),Theme(output),60,2,'')
    view.run(io.StringIO('\nq\n'),output,None)
    assert 'DISCONNECTED' in output.getvalue() and 'GET /items' in output.getvalue()
    view.run(io.StringIO(''),output,None)


@pytest.mark.parametrize('error, code', [(KeyboardInterrupt,130),(EOFError,0)])
def test_monitor_restores_on_interruption(monkeypatch, error, code):
    class TTY(io.StringIO):
        def isatty(self):
            return True
    monkeypatch.setattr(Monitor,'run',Mock(side_effect=error))
    args = SimpleNamespace(output=None,json=False,once=False,plain=True,window=60,interval=2,filter='')
    assert run(lambda _:snapshot(), args, (OSError,), stdin=TTY(),stdout=TTY()) == code


def test_windows_key_decoder(monkeypatch):
    from autotuner import terminal_ui
    if sys.platform != 'win32':
        pytest.skip('Windows console decoder')
    import msvcrt
    keys = iter(['\xe0','P','\r','\t','\x03','\x1a'])
    monkeypatch.setattr(msvcrt,'kbhit', lambda:True)
    monkeypatch.setattr(msvcrt,'getwch', lambda:next(keys))
    keyboard = Keyboard(io.StringIO())
    with keyboard.active():
        assert [keyboard.read(0) for _ in range(3)] == ['down','enter','tab']
        with pytest.raises(KeyboardInterrupt):
            keyboard.read(0)
        with pytest.raises(EOFError):
            keyboard.read(0)
    monkeypatch.setattr(msvcrt,'kbhit',lambda:False)
    assert keyboard.read(0) is None
    assert terminal_ui.terminal_size()[0] <= 132


def test_plain_guided_baseline_and_compare_dispatch_arguments(tmp_path):
    class TTY(io.StringIO):
        def isatty(self):
            return True
    def dispatch(args):
        if args[-1]=='--json':
            if '--apply' in args:
                print(json.dumps(dict(state='completed', findings=0, output='after.json')))
            else:
                workload = dict(method='GET', path='/items', requests=8, warmup=4, environment='local',
                                delay_ms=100, timeout_seconds=3, concurrency=1, expected_status=200)
                print(json.dumps(dict(review_token='preview',workload=workload)))
        return 0
    invoke = Mock(side_effect=dispatch)
    keys = '2\n5\n9\n2\n/items\napp-v1\n\n8\n4\nafter.json\n1\nb\n4\n5\nbefore.json\nafter.json\nresult.json\nq\n'
    out = TTY()
    assert terminal.run(project=tmp_path, invoke=invoke, stdin=TTY(keys), stdout=out,plain=True)==0
    preview, active, comparison = [item.args[0] for item in invoke.call_args_list]
    assert '--requests=8' in preview and '--warmup=4' in preview
    assert '--stage=optimized' in preview and '--environment=local' in preview
    assert active[-3:] == ['--apply','preview','--json']
    assert comparison[1:4] == ['compare',str(tmp_path/'before.json'),str(tmp_path/'after.json')]
    assert '4 warmup and 8 measured requests' in out.getvalue()
    assert 'Choose a listed number.' in out.getvalue()
    assert 'Capture saved' in out.getvalue() and 'review_token' not in out.getvalue()
    assert 'Comparison saved to' in out.getvalue()


def test_menu_show_report_uses_summary_and_keeps_export_action(tmp_path):
    class TTY(io.StringIO):
        def isatty(self):
            return True
    data = dict(effective_mode='observe', session_id='sample-session', started_at='2026-11-23T14:00:00Z',
                routes=[dict(boundary='proxy', method='GET', route='/items', count=8, p95_ms=1.2345,
                             cache_hits=3, error_rate=0)], tuning_decisions=[],
                sampling={'sampling_may_be_incomplete': True})
    def dispatch(args):
        if args[-1] == '--json':
            print(json.dumps(data))
        return 0
    invoke = Mock(side_effect=dispatch)
    out = TTY()
    assert terminal.run(project=tmp_path, invoke=invoke, stdin=TTY('4\n2\n3\nraw.json\nq\n'), stdout=out) == 0
    assert 'GET /items | 8 requests | p95 1.23 ms' in out.getvalue()
    assert 'last committed checkpoint' in out.getvalue() and '"routes"' not in out.getvalue()
    assert invoke.call_args_list[0].args[0][-1] == '--json'
    assert invoke.call_args.args[0][-2:] == ['--output', str(tmp_path/'raw.json')]


def test_fullscreen_reports_and_diagnostics_stay_inside_workspace(tmp_path, monkeypatch, capsys):
    out = io.StringIO()
    data = dict(effective_mode='observe', session_id='saved-session', started_at='today', routes=[
        dict(boundary='proxy', method='GET', route='/items', count=8, p95_ms=1.2345, error_rate=0, cache_hits=2)],
        sampling={}, tuning_decisions=[])

    def invoke(args):
        assert args[-1] == '--json'
        print(json.dumps(data))
        print('Test diagnostic', file=sys.stderr)
        return 0

    workspace = terminal.Workspace(tmp_path,tmp_path/'base.json',invoke,io.StringIO(),out)
    workspace.theme.rich = True
    workspace.keyboard = SimpleNamespace(active=nullcontext, read=Mock(side_effect=['4','2','d','enter','q']))
    monkeypatch.setattr(terminal,'terminal_size',lambda:(105,25))
    assert workspace.loop() == 0
    screen = out.getvalue()
    assert screen.count('\x1b[?1049h') == screen.count('\x1b[?1049l') == 1
    assert 'GET /items' in screen and 'P95 ms' in screen and 'Test diagnostic' in screen
    assert 'DETAILS' in screen and '"routes"' not in screen
    captured = capsys.readouterr()
    assert not captured.out and not captured.err


def test_fullscreen_form_can_edit_input_and_return_to_menu(tmp_path, monkeypatch):
    out = io.StringIO()
    workspace = terminal.Workspace(tmp_path,tmp_path/'base.json',Mock(),io.StringIO(),out)
    workspace.theme.rich = True
    workspace.keyboard = SimpleNamespace(active=nullcontext, read=Mock(
        side_effect=['c','A','b','x','erase','.','j','s','o','n','enter','q']))
    monkeypatch.setattr(terminal,'terminal_size',lambda:(90,24))
    assert workspace.loop() == 0
    assert workspace.config == tmp_path/'Ab.json'
    assert 'AUTOTUNER / Select config' in out.getvalue()


def test_fullscreen_baseline_uses_numbered_choices_and_keeps_path_fields(tmp_path, monkeypatch):
    from autotuner.terminal_ui import Panel

    out = io.StringIO()
    workspace = terminal.Workspace(tmp_path, tmp_path/'config.json', Mock(), io.StringIO(), out)
    workspace.theme.rich = True
    workspace.panel = Panel('Capture matched GET workload')
    keys = ['x', '2', *'/items/2', 'enter', *'app-v1', 'enter', 'down', 'enter',
            'enter', 'enter', *'After Run.json', 'enter']
    workspace.keyboard = SimpleNamespace(active=nullcontext, read=Mock(side_effect=keys))
    review = Mock()
    monkeypatch.setattr(workspace, 'review', review)
    monkeypatch.setattr(terminal, 'terminal_size', lambda:(95,24))
    workspace.guided_baseline(['baseline'])
    review.assert_called_once_with(['baseline', '--stage=optimized', '--get=/items/2', '--app-state=app-v1',
                                    '--environment=staging', '--requests=10', '--warmup=0',
                                    '--output='+str(tmp_path/'After Run.json')])
    screen = out.getvalue()
    assert all(label in screen for label in ('1  Baseline', '2  Optimized', '1  Local', '2  Staging', '> /items/2'))


@pytest.mark.parametrize('key', ('back', 'enter', '2'))
def test_fullscreen_approval_requires_an_explicit_yes(tmp_path, monkeypatch, key):
    from autotuner.terminal_ui import Panel

    workspace = terminal.Workspace(tmp_path, tmp_path/'config.json', Mock(), io.StringIO(), io.StringIO())
    workspace.theme.rich = True
    workspace.panel = Panel('Approval')
    workspace.keyboard = SimpleNamespace(active=nullcontext, read=Mock(side_effect=[key, '1']))
    monkeypatch.setattr(terminal, 'terminal_size', lambda:(80,24))
    assert not workspace.confirm('Apply preview?')
    assert workspace.confirm('Apply preview?')


def test_table_wrap_preserves_full_cell_at_narrow_width():
    from autotuner.terminal_ui import Table, cell_width
    table = Table(('Operation','P95 ms','Result'), [('GET /items/測試', '1.25', 'Approved for repeated public reads')])
    for width in (16, 40, 82):
        lines = list(table.lines(width))
        assert all(cell_width(line) <= width for line in lines)
        assert '1.25' in '\n'.join(lines)
        assert 'reads' in '\n'.join(lines)


def test_workspace_stops_only_its_owned_services(tmp_path, monkeypatch):
    from autotuner import benchmarking
    from autotuner.terminal_ui import Panel
    proxy, restarted, demo = Mock(), Mock(), Mock()
    child = Mock(side_effect=[proxy,restarted,demo])
    monkeypatch.setattr(benchmarking, 'Child', child)
    workspace = terminal.Workspace(tmp_path,tmp_path/'base.json',Mock(),io.StringIO(),io.StringIO())
    workspace.panel = Panel('Services')
    monkeypatch.setattr(workspace, 'structured', lambda args: {'config': {
        'mode':'observe', 'listen':'127.0.0.1:8080', 'upstream':'http://127.0.0.1:3000'}})
    monkeypatch.setattr(workspace, 'confirm', lambda _:True)
    for _ in range(2):
        workspace.start_proxy(['serve','--config',str(workspace.config)])
    proxy.close.assert_called_once()
    workspace.toggle_demo()
    monkeypatch.setattr(workspace, 'navigate', lambda:0)
    assert workspace.loop() == 0
    restarted.close.assert_called_once()
    demo.close.assert_called_once()
    assert child.call_args_list[0].args == ('workspace-proxy',workspace.config)
    assert child.call_args.kwargs['port'] == 3000


def test_saved_comparison_is_a_table_inside_the_workspace(tmp_path, monkeypatch):
    from autotuner.terminal_ui import Panel, Table

    saved = tmp_path/'capture.json'
    write_new(saved, compare(matched('baseline'), matched('optimized')))
    workspace = terminal.Workspace(tmp_path,tmp_path/'base.json',Mock(),io.StringIO(),io.StringIO())
    workspace.panel = Panel('Open a saved capture')
    workspace.open_capture(saved)
    table = workspace.panel.content[0]
    assert isinstance(table, Table)
    assert ('Mean', '10.00 ms', '2.00 ms', '+80.0%') in table.rows
    assert ('Cache hits', '0', '2', '-') in table.rows
    assert workspace.panel.details
    broken = tmp_path/'broken.json'
    write_new(broken, dict(schema_version=1, kind='matched-read-comparison', comparable=True))
    workspace.open_capture(broken)
    assert 'invalid structure' in workspace.panel.content[-1]


def test_workspace_proxy_binds_selected_listener(tmp_path, monkeypatch):
    from autotuner import qualification_server

    config = tmp_path/'base.json'
    write_new(config, {'upstream':'http://127.0.0.1:3000', 'listen':'[::1]:8092'})
    monkeypatch.delenv('AUTOTUNER_LISTEN', raising=False)
    monkeypatch.setattr(sys,'argv',['qualification_server','--kind','workspace-proxy','--path',str(config)])
    inspect = Mock()
    monkeypatch.setattr(qualification_server.uvicorn.Server,'run',lambda self:inspect(self.config))
    qualification_server.main()
    server = inspect.call_args.args[0]
    assert (server.host, server.port) == ('::1',8092)
    assert server.app.config.listen == '[::1]:8092'

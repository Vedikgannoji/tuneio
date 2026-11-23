"""Terminal actions stay explicit, bounded and usable without a browser."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from unittest.mock import Mock

import pytest

from autotuner import cli,terminal


class TTY(io.StringIO):
    def isatty(self):return True


@pytest.fixture(autouse=True)
def clean_config_env(monkeypatch):
    monkeypatch.delenv('AUTOTUNER_CONFIG',raising=False)


def run_menu(tmp_path,text='',invoke=None,**kwargs):
    out,err=TTY(),io.StringIO()
    code=terminal.run(project=str(tmp_path),invoke=invoke or Mock(return_value=0),stdin=TTY(text),stdout=out,stderr=err,**kwargs)
    return code,out.getvalue(),err.getvalue()


def test_no_argument_modes_do_not_read_redirected_input(monkeypatch,capsys):
    stdin=Mock();stdin.isatty.return_value=False;stdin.readline.side_effect=AssertionError('Unexpected prompt')
    monkeypatch.setattr(sys,'stdin',stdin)
    assert cli.main([])==0
    assert 'menu' in capsys.readouterr().out
    stdin.readline.assert_not_called()


def test_bare_command_opens_menu_only_with_two_terminals(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path);out=TTY();monkeypatch.setattr(sys,'stdout',out);monkeypatch.setattr(sys,'stdin',TTY('q\n'))
    assert cli.main([])==0
    assert 'Terminal workspace' in out.getvalue() and not list(tmp_path.iterdir())


@pytest.mark.parametrize('args',[['--no-input'],['--no-input','menu'],['menu','--no-input'],['menu']])
def test_noninteractive_entry_never_prompts(tmp_path,monkeypatch,args):
    monkeypatch.chdir(tmp_path)
    stdin=Mock();stdin.isatty.return_value=False;stdin.readline.side_effect=AssertionError('Unexpected prompt')
    monkeypatch.setattr(sys,'stdin',stdin)
    assert cli.main(args)==(0 if args==['--no-input'] else 2)
    stdin.readline.assert_not_called()


def test_json_manifest_does_not_scan_read_config_or_connect(tmp_path,monkeypatch,capsys):
    (tmp_path/'autotuner.json').write_text('not a config')
    monkeypatch.setattr(Path,'read_text',Mock(side_effect=AssertionError('No project file reads')))
    monkeypatch.setattr(os,'scandir',Mock(side_effect=AssertionError('No scanning')))
    monkeypatch.setattr(cli.socket,'create_connection',Mock(side_effect=AssertionError('No network')))
    monkeypatch.setattr(cli.webbrowser,'open',Mock(side_effect=AssertionError('No browser')))
    assert cli.main(['--no-input','menu','--project',str(tmp_path),'--json'])==0
    data=json.loads(capsys.readouterr().out)
    assert data['schema_version']==1 and data['read_only_on_entry'] is True and data['interactive'] is False
    assert len(data['groups'])==4 and data['planned_features']==[]
    assert 'phase' not in data


@pytest.mark.parametrize('group,key,answers,prefix',[
    ('1','1','', ['config','check']),('1','2','\nyes\n',['init']),
    ('1','3','yes\n',['serve']),('1','4','',['doctor']),('1','5','',['doctor','--connect']),
    ('2','1','',['status']),('2','2','',['endpoints']),('2','3','',['insights']),
    ('2','4','baseline.json\n',['baseline']),
    ('3','1','/api/products\n',['cache','explain']),('3','2','yes\n',['cache','clear']),
    ('4','1','',['sessions']),('4','2','',['report','--format','json']),
    ('4','3','export.json\n',['report','--format','json']),('4','4','export.json\n',['report','--live','--format','json']),
])
def test_available_actions_dispatch_argv_without_shell(tmp_path,group,key,answers,prefix):
    invoke=Mock(return_value=0)
    code,out,err=run_menu(tmp_path,group+'\n'+key+'\n'+answers+'q\n',invoke)
    assert code==0 and not err and 'Action completed.' in out
    invoke.assert_called_once()
    args=invoke.call_args.args[0]
    assert args[:len(prefix)+1]==['--no-input',*prefix]
    assert args[args.index('--config')+1]==str(tmp_path/'autotuner.json')
    if key=='3' and group=='4' or key=='4' and group=='4':
        assert args[args.index('--output')+1]==str(tmp_path/'export.json')


@pytest.mark.parametrize('group,key,answers',[
    ('1','2','b\n'),('1','2','\nn\n'),('1','3','n\n'),('3','2','\n'),
    ('3','1','b\n'),('4','3','b\n'),('4','3','\n'),
])
def test_cancelled_or_unconfirmed_actions_do_nothing(tmp_path,group,key,answers):
    invoke=Mock(return_value=0)
    assert run_menu(tmp_path,group+'\n'+key+'\n'+answers+'q\n',invoke)[0]==0
    invoke.assert_not_called()


@pytest.mark.parametrize('route',['--version','/items?secret=value','/items#fragment','/bad\x1bpath','/'+'x'*1024])
def test_route_input_is_validated_before_control_request(tmp_path,route):
    invoke=Mock(return_value=0)
    code,out,_=run_menu(tmp_path,'3\n1\n'+route+'\nq\n',invoke)
    assert code==0 and 'Enter a route path' in out
    invoke.assert_not_called()


def test_navigation_help_and_invalid_choice(tmp_path):
    invoke=Mock(return_value=0)
    code,out,err=run_menu(tmp_path,'unknown\nh\n1\nh\nb\n2\n0\n0\n',invoke)
    assert code==0 and not err and 'Choose a listed key' in out and 'Monitor shows live traffic' in out
    invoke.assert_not_called()


def test_project_and_config_selection_do_not_change_process_directory(tmp_path):
    next_project=tmp_path/'my project';next_project.mkdir();cwd=Path.cwd();invoke=Mock(return_value=0)
    code,out,err=run_menu(tmp_path,'p\nmy project\nc\ncustom.json\n2\n1\nq\n',invoke)
    assert code==0 and not err and Path.cwd()==cwd
    assert str(next_project/'custom.json') in out
    assert invoke.call_args.args[0][-1]==str(next_project/'custom.json')


def test_invalid_and_cancelled_path_selection(tmp_path):
    code,out,_=run_menu(tmp_path,'p\nb\nc\n\np\nmissing\nc\nbad\x1bname\nq\n')
    assert code==0 and out.count('Cannot select that path')==2


def test_config_precedence(tmp_path,monkeypatch):
    monkeypatch.setenv('AUTOTUNER_CONFIG','environment.json')
    code,out,_=run_menu(tmp_path,as_json=True)
    assert code==0 and json.loads(out)['config']==str(tmp_path/'environment.json')
    code,out,_=run_menu(tmp_path,as_json=True,config='explicit.json')
    assert json.loads(out)['config']==str(tmp_path/'explicit.json')


def test_create_and_validate_use_existing_cli_with_no_network(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path);out=TTY();monkeypatch.setattr(sys,'stdout',out)
    monkeypatch.setattr(sys,'stdin',TTY('1\n2\nhttp://127.0.0.1:3000\nyes\n1\nb\nq\n'))
    network=Mock(side_effect=AssertionError('Unexpected connection'));monkeypatch.setattr(cli.socket,'create_connection',network)
    assert cli.main(['menu'])==0
    config=json.loads((tmp_path/'autotuner.json').read_text())
    assert config['mode']=='observe' and config['cache']['routes']==[]
    assert 'valid: True' in out.getvalue()
    network.assert_not_called()


def test_create_refuses_to_overwrite_existing_configuration(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path);path=tmp_path/'autotuner.json';path.write_text('preserve me')
    monkeypatch.setattr(sys,'stdout',TTY());monkeypatch.setattr(sys,'stdin',TTY('1\n2\n\ny\nq\n'))
    assert cli.main(['menu'])==0 and path.read_text()=='preserve me'


def test_missing_config_actions_explain_recovery(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sys, 'stdin', TTY('1\n1\n3\ny\nb\n3\n2\ny\nq\n'))
    output = TTY()
    monkeypatch.setattr(sys, 'stdout', output)
    network = Mock(side_effect=AssertionError('Unexpected connection'))
    monkeypatch.setattr(cli.urllib.request, 'build_opener', network)

    assert cli.main(['menu', '--project', str(tmp_path)]) == 0
    errors = capsys.readouterr().err
    assert errors.count('Configuration file not found:') == 3
    assert 'Setup > Create observe configuration' in errors
    assert 'c to select a config' in errors
    assert output.getvalue().count('exit 2') == 3
    assert not list(tmp_path.iterdir())
    network.assert_not_called()


@pytest.mark.parametrize('failure',[OSError('secret'),ValueError('secret')])
def test_action_errors_are_sanitized_and_not_retried(tmp_path,failure):
    invoke=Mock(side_effect=failure)
    code,out,err=run_menu(tmp_path,'2\n1\nq\n',invoke)
    assert code==0 and 'secret' not in out+err and 'No automatic retry' in out
    invoke.assert_called_once()


def test_failed_action_returns_to_menu(tmp_path):
    code,out,_=run_menu(tmp_path,'2\n1\nq\n',Mock(return_value=1))
    assert code==0 and 'exit 1' in out


def test_eof_and_keyboard_interrupt_are_clean(tmp_path):
    assert run_menu(tmp_path,'')[0]==0
    stdin=TTY();stdin.readline=Mock(side_effect=KeyboardInterrupt())
    out,err=TTY(),io.StringIO()
    assert terminal.run(project=str(tmp_path),invoke=Mock(),stdin=stdin,stdout=out,stderr=err)==130
    assert 'cancelled' in err.getvalue()


@pytest.mark.parametrize('prefix',['','3\n1\n'])
def test_oversized_input_aborts_instead_of_consuming_remainder_as_commands(tmp_path,prefix):
    invoke=Mock(return_value=0)
    assert run_menu(tmp_path,prefix+'x'*(terminal.MAX_INPUT+1)+'\n3\n2\ny\n',invoke)[0]==2
    invoke.assert_not_called()


def test_bad_project_nonterminal_output_and_io_errors(tmp_path):
    assert run_menu(tmp_path/'missing',as_json=True)[0]==2
    assert run_menu(tmp_path,config='bad\x1bname')[0]==2
    err=io.StringIO();out=io.StringIO();stdin=TTY()
    assert terminal.run(project=str(tmp_path),invoke=Mock(),stdin=stdin,stdout=out,stderr=err)==2
    stdin.readline=Mock(side_effect=OSError('secret'))
    err=io.StringIO()
    assert terminal.run(project=str(tmp_path),invoke=Mock(),stdin=stdin,stdout=TTY(),stderr=err)==2
    assert 'secret' not in err.getvalue()


def test_project_display_cannot_inject_terminal_controls():
    assert '\x1b' not in terminal.display('project\x1b[31m\nname')
    assert len(terminal.display('x'*5000))==1024


@pytest.mark.parametrize('arguments',[['--help'],['--version'],['--no-input'],['menu','--json']])
def test_light_entrypoints_do_not_import_runtime_stack(tmp_path,arguments):
    root=str(Path(__file__).resolve().parents[1])
    code='import sys; sys.path.insert(0,'+repr(root)+'); from autotuner import cli; '\
         +'\ntry: cli.main('+repr(arguments)+')'\
         +'\nexcept SystemExit as exc: assert exc.code==0\n'
    code+='assert not any(n in sys.modules for n in ("httpx","uvicorn","fastapi","pydantic","sqlite3","autotuner.session"))'
    result=subprocess.run([sys.executable,'-I','-c',code],cwd=tmp_path,capture_output=True,text=True,timeout=15)
    assert result.returncode==0,result.stderr

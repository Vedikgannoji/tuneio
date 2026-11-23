from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

from scripts import smoke_wheel


def test_clean_install_coordinator_uses_own_venv_and_isolated_imports(tmp_path,monkeypatch):
    wheel=tmp_path/'artifact.whl';wheel.write_bytes(b'placeholder')
    create=Mock();run=Mock()
    monkeypatch.setattr(smoke_wheel.venv,'create',create)
    monkeypatch.setattr(smoke_wheel.subprocess,'run',run)
    monkeypatch.setattr(sys,'argv',['smoke_wheel',str(wheel)])
    smoke_wheel.main()
    assert create.call_count==1 and create.call_args.kwargs['with_pip']
    assert create.call_args.kwargs['system_site_packages'] is False
    assert run.call_count==12
    assert Path(run.call_args_list[-1].args[0][-1]).name=='smoke_product.py'
    for call in run.call_args_list:
        assert call.kwargs['check'] is True and call.kwargs['timeout']==180
        assert Path(call.args[0][0]).is_relative_to(call.kwargs['cwd'])
    assert run.call_args_list[0].args[0][1:]==['-m','pip','install',str(wheel)]
    assert all(call.args[0][1]=='-I' for call in run.call_args_list[1:])
    assert run.call_args_list[2].args[0][1:5]==['-I','-m','autotuner','menu']


def test_local_only_check_distinguishes_system_dependencies_and_no_network(tmp_path,monkeypatch):
    wheel=tmp_path/'artifact.whl';wheel.write_bytes(b'placeholder')
    create,run=Mock(),Mock()
    monkeypatch.setattr(smoke_wheel.venv,'create',create)
    monkeypatch.setattr(smoke_wheel.subprocess,'run',run)
    monkeypatch.setattr(sys,'argv',['smoke_wheel',str(wheel),'--local-only'])
    smoke_wheel.main()
    assert create.call_args.kwargs['system_site_packages'] is True
    assert '--no-deps' in run.call_args_list[0].args[0]
    assert 'smoke_product.py' not in str(run.call_args_list)


def test_clean_install_missing_wheel_does_not_start_environment(tmp_path,monkeypatch):
    create=Mock();monkeypatch.setattr(smoke_wheel.venv,'create',create)
    monkeypatch.setattr(sys,'argv',['smoke_wheel',str(tmp_path/'missing.whl')])
    with pytest.raises(FileNotFoundError):smoke_wheel.main()
    create.assert_not_called()


@pytest.mark.parametrize('count', [0, 1, 2])
def test_clean_install_selects_one_wheel_from_directory(tmp_path, monkeypatch, count):
    for index in range(count): (tmp_path / f'candidate{index}.whl').write_bytes(b'placeholder')
    create=Mock();run=Mock()
    monkeypatch.setattr(smoke_wheel.venv,'create',create)
    monkeypatch.setattr(smoke_wheel.subprocess,'run',run)
    monkeypatch.setattr(sys,'argv',['smoke_wheel',str(tmp_path)])
    if count == 1:
        smoke_wheel.main()
        assert run.call_args_list[0].args[0][-1] == str(tmp_path / 'candidate0.whl')
    else:
        with pytest.raises(ValueError): smoke_wheel.main()
        create.assert_not_called()

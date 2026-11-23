import json
from pathlib import Path

from tuneio import cli
from tuneio.product_config import load_config


def test_review_lifecycle_preserves_parent_and_storage(tmp_path, capsys):
    root = tmp_path / 'base.json'
    root.write_text(json.dumps(dict(upstream='http://origin', operations=[
        {'method':'GET','path':'/products'}, {'method':'PATCH','path':'/products/*'}],
        cache={'routes':[{'match':'/other','id':'other','policy':'cache'}]})))
    original = root.read_bytes()

    def revision(action, source, dest, *options):
        cmd = ['optimize', action, '--config', str(source), '--output', str(dest), *options, '--json']
        assert cli.main(cmd) == 0
        preview = json.loads(capsys.readouterr().out)
        assert not dest.exists() and not preview['written']
        assert cli.main(cmd + ['--apply', preview['review_token']]) == 0
        assert json.loads(capsys.readouterr().out)['written']
        assert cli.main(cmd + ['--apply', preview['review_token']]) == 2
        capsys.readouterr()
        return load_config(dest)

    enabled = tmp_path / 'active.json'
    config = revision('enable', root, enabled, '--route','/products')
    assert config.cache.routes[0].policy == 'observe'
    rule = config.cache.routes[1]
    assert rule.policy == 'adaptive' and rule.invalidate_on[0].path == '/products/*'
    assert Path(config.metrics.storage_path).is_absolute()
    assert revision('pause', enabled, tmp_path/'pause.json').mode == 'recommend'
    assert revision('disable', enabled, tmp_path/'off.json').mode == 'off'
    restored = revision('rollback', enabled, tmp_path/'restored.json')
    assert restored.cache.routes == load_config(root).cache.routes and restored.mode == 'observe'
    assert root.read_bytes() == original


def test_revision_rejections(tmp_path, capsys):
    base = tmp_path/'base.json'
    base.write_text('{"upstream":"http://origin","operations":[{"method":"GET","path":"/x"}]}')
    common = ['--config',str(base),'--output',str(tmp_path/'next.json'),'--json']
    assert cli.main(['optimize','rollback',*common]) == 2
    assert cli.main(['optimize','enable','--route','/other','--read-only',*common]) == 2
    cmd = ['optimize','enable','--route','/x',*common]
    assert cli.main(cmd) == 2
    assert cli.main(cmd+['--read-only','--apply','wrong']) == 2
    assert not (tmp_path/'next.json').exists()
    assert cli.main(cmd+['--read-only']) == 0
    token = json.loads(capsys.readouterr().out)['review_token']
    assert cli.main(cmd+['--read-only','--apply',token]) == 0
    file = tmp_path/'next.json'
    document = json.loads(file.read_text())
    document['config']['upstream'] = 'http://other'
    file.write_text(json.dumps(document))
    assert cli.main(['optimize','inspect','--config',str(file)]) == 2

import json

import pytest
from pydantic import ValidationError

from tuneio.product_config import ProductConfig, load_config


def test_example_and_defaults():
    config = load_config('examples/autotuner.json')
    assert config.mode == 'observe'
    assert config.limits.upstream_concurrency == 8
    assert config.cache.routes[0].policy == 'adaptive'  # global observe still wins
    assert ProductConfig(upstream='https://api.example.com/').upstream == 'https://api.example.com'
    assert ProductConfig(upstream='http://[::1]:3000', listen='[::1]:8080').listen == '[::1]:8080'


@pytest.mark.parametrize('value', ['ftp://example.com', 'http://user:secret@example.com',
                                  'http://example.com/api', 'http://example.com?token=secret',
                                  'http://example.com#fragment', 'http://example.com:99999',
                                  'http://example.com:0', 'http://bad host', 'http://[bad', 'http://bad\\host'])
def test_invalid_upstream(value):
    with pytest.raises(ValidationError):
        ProductConfig(upstream=value)


@pytest.mark.parametrize('value', ['0.0.0.0:8080', 'localhost:8080', '127.0.0.1:0', '127.0.0.1:65536', 'bad'])
def test_invalid_listener(value):
    with pytest.raises(ValidationError):
        ProductConfig(upstream='http://localhost:3000', listen=value)


@pytest.mark.parametrize('fields', [
    {'schema_version': 2}, {'mode': 'magic'}, {'unexpected': True},
    {'limits': {'upstream_concurrency': True}}, {'limits': {'cache_bytes': 1}},
    {'cache': {'routes': [{'id': 'x', 'match': '/x', 'methods': ['POST']}]}},
    {'cache': {'routes': [{'id': 'x', 'match': '/x', 'ttl_min_seconds': 50}]}},
    {'cache': {'routes': [{'id': 'x', 'match': '/x', 'vary_headers': ['Accept', 'Accept']}]}},
    {'cache': {'routes': [{'id': 'x', 'match': '/x', 'vary_headers': ['Authorization']}]}},
])
def test_invalid_contract(fields):
    with pytest.raises(ValidationError):
        ProductConfig.model_validate(dict(upstream='http://localhost:3000', **fields))


def test_matching_ambiguity_and_specificity():
    base = {'upstream': 'http://localhost:3000'}
    for routes in [[{'id': 'same', 'match': '/a'}, {'id': 'same', 'match': '/b'}],
                   [{'id': 'a', 'match': '/a/*'}, {'id': 'b', 'match': '/*/b'}]]:
        with pytest.raises(ValidationError):
            ProductConfig(**base, cache={'routes': routes})
    config = ProductConfig(**base, cache={'routes': [{'id': 'a', 'match': '/products/*'},
                                                     {'id': 'b', 'match': '/products/featured'}]})
    assert len(config.cache.routes) == 2


def test_invalid_json_and_missing_file(tmp_path):
    path = tmp_path / 'config.json'
    with pytest.raises(FileNotFoundError):
        load_config(path)
    path.write_text('{broken')
    with pytest.raises(json.JSONDecodeError):
        load_config(path)


def test_operation_labels_are_independent_of_cache_rules():
    config = ProductConfig(upstream='http://localhost:3000', operations=[
        {'method': 'POST', 'path': '/items'}, {'method': 'GET', 'path': '/items/*'}])
    assert config.cache.routes == [] and len(config.operations) == 2
    with pytest.raises(ValidationError):
        ProductConfig(upstream='http://localhost:3000', operations=[
            {'method': 'GET', 'path': '/items/*'}, {'method': 'GET', 'path': '/*/one'}])

"""Version one product configuration contract."""
import ipaddress
import json
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Positive = Annotated[int, Field(strict=True, gt=0)]
Pattern = Annotated[str, Field(max_length=1024, pattern=r'^/(?:[A-Za-z0-9_.~-]+|\*)(?:/(?:[A-Za-z0-9_.~-]+|\*))*$|^/$')]


class Contract(BaseModel):
    model_config = ConfigDict(extra='forbid', validate_default=True)


class Limits(Contract):
    upstream_concurrency: Positive = 8
    queue_capacity: Positive = 64
    request_body_bytes: Positive = 8388608
    header_bytes: Positive = 65536
    connect_timeout_seconds: Positive = 5
    read_timeout_seconds: Positive = 30
    response_timeout_seconds: Positive = 60
    cache_bytes: Positive = 67108864
    cache_entries: Positive = 256
    cache_entry_bytes: Positive = 1048576

    @model_validator(mode='after')
    def entry_budget(self):
        if self.cache_entry_bytes > self.cache_bytes:
            raise ValueError('cache_entry_bytes cannot exceed cache_bytes')
        return self


class MetricsConfig(Contract):
    raw_capacity: Positive = 10000
    retention_days: Positive = 7
    storage_max_bytes: Annotated[int, Field(strict=True, ge=131072)] = 104857600
    storage_path: Annotated[str, Field(min_length=1, max_length=4096)] = ".autotuner/metrics.sqlite3"
    writer_queue_capacity: Annotated[int, Field(strict=True, ge=1, le=10000)] = 1024

    @field_validator("storage_path")
    @classmethod
    def local_storage(cls, value):
        if "\x00" in value or value == ":memory:" or value.startswith("file:"):
            raise ValueError("storage_path must name a local SQLite file")
        return value


class Mutation(Contract):
    method: Literal['POST', 'PUT', 'PATCH', 'DELETE']
    path: Pattern


class Operation(Contract):
    method: Literal['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS']
    path: Pattern


class RoutePolicy(Contract):
    id: Annotated[str, Field(pattern=r'^[a-z][a-z0-9-]{0,63}$')]
    match: Pattern
    methods: list[Literal['GET']] = Field(default_factory=lambda: ['GET'], min_length=1, max_length=1)
    policy: Literal['observe', 'cache', 'adaptive'] = 'observe'
    ttl_min_seconds: Positive = 2
    ttl_max_seconds: Positive = 30
    vary_headers: list[Literal['Accept', 'Accept-Encoding']] = Field(default_factory=list)
    invalidate_on: list[Mutation] = Field(default_factory=list)

    @model_validator(mode='after')
    def bounds(self):
        if self.ttl_max_seconds > 10**308:
            raise ValueError('TTL exceeds the supported numeric range')
        if self.ttl_min_seconds > self.ttl_max_seconds:
            raise ValueError('TTL minimum cannot exceed maximum')
        if len(set(self.vary_headers)) != len(self.vary_headers):
            raise ValueError('Duplicate variant header')
        return self


class CacheRules(Contract):
    routes: list[RoutePolicy] = Field(default_factory=list, max_length=1000)

    @model_validator(mode='after')
    def unambiguous(self):
        ids = [route.id for route in self.routes]
        if len(ids) != len(set(ids)):
            raise ValueError('Route IDs must be unique')
        for i, first in enumerate(self.routes):
            a = first.match.split('/')
            for second in self.routes[i + 1:]:
                b = second.match.split('/')
                same_specificity = a.count('*') == b.count('*')
                overlap = len(a) == len(b) and all(x == y or '*' in (x, y) for x, y in zip(a, b))
                if same_specificity and overlap:
                    raise ValueError(f'Ambiguous routes: {first.id} and {second.id}')
        return self


class TuningConfig(Contract):
    sample_capacity: Annotated[int, Field(strict=True, ge=2, le=100)] = 20
    minimum_samples: Annotated[int, Field(strict=True, ge=1, le=100)] = 4
    reuse_minimum: Annotated[int, Field(strict=True, ge=2, le=100)] = 2
    enable_ms: Annotated[float, Field(strict=True, gt=0, le=60000, allow_inf_nan=False)] = 2.0
    retain_ms: Annotated[float, Field(strict=True, gt=0, le=60000, allow_inf_nan=False)] = 1.5
    cooldown_seconds: Annotated[float, Field(strict=True, ge=0, le=3600, allow_inf_nan=False)] = 5.0
    reuse_window_seconds: Annotated[float, Field(strict=True, gt=0, le=3600, allow_inf_nan=False)] = 60.0
    learning_window_seconds: Annotated[float, Field(strict=True, gt=0, le=86400, allow_inf_nan=False)] = 300.0
    history_capacity: Annotated[int, Field(strict=True, ge=1, le=1000)] = 100

    @model_validator(mode='after')
    def bounds(self):
        if self.minimum_samples>self.sample_capacity or self.reuse_minimum>self.sample_capacity or self.retain_ms>self.enable_ms:
            raise ValueError('Invalid learning capacity or hysteresis thresholds')
        return self


class ProductConfig(Contract):
    schema_version: Literal[1] = 1
    upstream: str
    listen: str = '127.0.0.1:8080'
    mode: Literal['observe', 'recommend', 'cache', 'adaptive', 'off'] = 'observe'
    operations: list[Operation] = Field(default_factory=list, max_length=1000)
    limits: Limits = Field(default_factory=Limits)
    cache: CacheRules = Field(default_factory=CacheRules)
    metrics: MetricsConfig = Field(default_factory=MetricsConfig)
    tuning: TuningConfig = Field(default_factory=TuningConfig)

    @model_validator(mode='after')
    def operation_labels(self):
        for i, first in enumerate(self.operations):
            a = first.path.split('/')
            for second in self.operations[i + 1:]:
                b = second.path.split('/')
                if (first.method == second.method and a.count('*') == b.count('*') and len(a) == len(b)
                    and all(x == y or '*' in (x, y) for x, y in zip(a, b))):
                    raise ValueError('Ambiguous operation labels')
        return self

    @field_validator('upstream')
    @classmethod
    def upstream_authority(cls, value):
        try:
            parsed = urlsplit(value)
            port = parsed.port
            valid = (parsed.scheme in ('http', 'https') and parsed.hostname and
                     not parsed.username and not parsed.password and not parsed.query and not parsed.fragment and
                     parsed.path in ('', '/') and (port is None or 1 <= port <= 65535) and
                     not any(c.isspace() for c in value) and '@' not in parsed.netloc and '\\' not in value)
        except ValueError:
            valid = False
        if not valid:
            raise ValueError('upstream must be an HTTP(S) authority without credentials, query, fragment or base path')
        return value.rstrip('/')

    @field_validator('listen')
    @classmethod
    def local_listener(cls, value):
        try:
            host, port = value.rsplit(':', 1)
            host = host.removeprefix('[').removesuffix(']')
            valid = (ipaddress.ip_address(host).is_loopback and port.isdigit() and 1 <= int(port) <= 65535
                     and (':' not in host or value.startswith('[')))
        except ValueError:
            valid = False
        if not valid:
            raise ValueError('listen must be a loopback IP and port, for example 127.0.0.1:8080 or [::1]:8080')
        return value


def load_config(path: str | Path) -> ProductConfig:
    """Parse and validate only; never opens a listener or contacts an upstream."""
    return resolve_storage(ProductConfig.model_validate(config_document(path)[0]), path)


def resolve_storage(config, path):
    storage_path = Path(config.metrics.storage_path)
    if not storage_path.is_absolute():
        config.metrics.storage_path = str((Path(path).resolve().parent / storage_path).resolve())
    return config


def config_document(path):
    from .optimization import CONFIG_LIMIT, unpack
    from .artifacts import read_json
    document, _ = read_json(path, CONFIG_LIMIT)
    return unpack(document)

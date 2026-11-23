from ._version import __version__
import os
import sqlite3
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi import Path as PathParam
from fastapi.responses import FileResponse, JSONResponse

from .database import Database
from .core import PerformanceEngine, Monitoring
from .schemas import CacheMode, OrderCreate, OrderStatus, ProductCreate, ProductPatch, UserCreate, UserPatch


def create_app(db_path=None, cache_config=None):
    app = FastAPI(title='Self-Optimizing API Performance Monitor', version=__version__)
    db = Database(db_path or os.getenv('AUTOTUNER_DB', 'autotuner.sqlite3'))
    engine = PerformanceEngine(cache_config)
    cache, metrics = engine.cache, engine.metrics
    app.state.engine = engine
    app.state.db, app.state.cache, app.state.metrics = db, cache, metrics
    app.add_middleware(Monitoring, metrics=metrics,
                       include=lambda scope: scope['path'].startswith('/api/'),
                       route_label=lambda scope: scope['route'].path if scope.get('route') else '/api/{unmatched}')

    @app.exception_handler(sqlite3.IntegrityError)
    async def integrity_error(request, exc):
        return JSONResponse(status_code=409, content={'detail': 'Duplicate value or invalid reference'})

    @app.exception_handler(sqlite3.OperationalError)
    async def database_error(request, exc):
        return JSONResponse(status_code=503, content={'detail': 'Database temporarily unavailable'})

    def found(value):
        if value is None:
            raise HTTPException(404, 'Resource not found')
        return value

    async def read(request, response, loader):
        result = await engine.read(request.scope['route'].path, request.url.path,
                                   request.query_params.multi_items(), loader,
                                   credentialed=bool(request.headers.get('authorization') or request.headers.get('cookie')))
        request.state.cache = result.outcome
        response.headers['X-Cache'] = result.outcome
        response.headers['X-Cache-Reason'] = result.reason
        response.headers['Cache-Control'] = 'no-store'
        return result.value

    async def mutate(loader, resources):
        prefixes = tuple('/api/' + resource for resource in resources)
        return found(await engine.write(loader, invalidate=prefixes))

    @app.get('/health')
    async def health():
        return {'status': 'ok'}

    @app.get('/', include_in_schema=False)
    async def dashboard():
        return FileResponse(Path(__file__).with_name('dashboard.html'), headers={'Cache-Control': 'no-store'})

    @app.get('/assets/workflow.js', include_in_schema=False)
    async def workflow_asset():
        return FileResponse(Path(__file__).with_name('workflow.js'), media_type='application/javascript',
                            headers={'Cache-Control': 'no-store'})

    @app.get('/api/users')
    async def users(request: Request, response: Response, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=2147483647)):
        return await read(request, response, lambda: db.list('users', limit, offset))

    @app.post('/api/users', status_code=201)
    async def create_user(body: UserCreate):
        return await mutate(lambda: db.create('users', body.model_dump()), ('users',))

    @app.get('/api/users/{item_id}')
    async def user(request: Request, response: Response, item_id: int = PathParam(ge=1, le=9223372036854775807)):
        return await read(request, response, lambda: found(db.get('users', item_id)))

    @app.patch('/api/users/{item_id}')
    async def update_user(body: UserPatch, item_id: int = PathParam(ge=1, le=9223372036854775807)):
        return await mutate(lambda: db.update('users', item_id, body.model_dump(exclude_unset=True)), ('users',))

    @app.get('/api/products')
    async def products(request: Request, response: Response, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=2147483647)):
        return await read(request, response, lambda: db.list('products', limit, offset))

    @app.post('/api/products', status_code=201)
    async def create_product(body: ProductCreate):
        return await mutate(lambda: db.create('products', body.model_dump()), ('products', 'reports'))

    @app.get('/api/products/{item_id}')
    async def product(request: Request, response: Response, item_id: int = PathParam(ge=1, le=9223372036854775807)):
        return await read(request, response, lambda: found(db.get('products', item_id)))

    @app.patch('/api/products/{item_id}')
    async def update_product(body: ProductPatch, item_id: int = PathParam(ge=1, le=9223372036854775807)):
        return await mutate(lambda: db.update('products', item_id, body.model_dump(exclude_unset=True)), ('products', 'reports'))

    @app.get('/api/orders')
    async def orders(request: Request, response: Response, limit: int = Query(50, ge=1, le=100), offset: int = Query(0, ge=0, le=2147483647)):
        return await read(request, response, lambda: db.list('orders', limit, offset))

    @app.post('/api/orders', status_code=201)
    async def create_order(body: OrderCreate):
        return await mutate(lambda: db.create('orders', body.model_dump()), ('orders', 'reports'))

    @app.get('/api/orders/{item_id}')
    async def order(request: Request, response: Response, item_id: int = PathParam(ge=1, le=9223372036854775807)):
        return await read(request, response, lambda: found(db.get('orders', item_id)))

    @app.patch('/api/orders/{item_id}/status')
    async def update_order(body: OrderStatus, item_id: int = PathParam(ge=1, le=9223372036854775807)):
        def transition():
            current = found(db.get('orders', item_id))
            transitions = {'pending': {'paid', 'cancelled'}, 'paid': {'shipped', 'cancelled'}, 'shipped': set(), 'cancelled': set()}
            if body.status != current['status'] and body.status not in transitions[current['status']]:
                raise HTTPException(409, 'Invalid order status transition')
            return db.update('orders', item_id, body.model_dump())
        return await mutate(transition, ('orders', 'reports'))

    @app.get('/api/reports/sales')
    async def sales(request: Request, response: Response):
        return await read(request, response, db.report)

    @app.get('/metrics')
    async def summary(window: int = Query(60, ge=1, le=3600)):
        return metrics.snapshot(window)

    @app.get('/metrics/comparison')
    async def comparison():
        return metrics.comparison()

    @app.get('/analysis')
    async def analysis():
        return engine.analyze()

    @app.put('/cache/mode')
    async def mode(body: CacheMode):
        cache.mode = body.mode
        cache.invalidate()
        return cache.status()

    @app.delete('/cache')
    async def clear():
        cache.invalidate()
        return {'cleared': True}

    return app

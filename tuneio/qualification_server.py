"""Private loopback subprocess entry point for benchmarks and the terminal workspace."""
import argparse
import asyncio
import json
import sys
import threading

import uvicorn

from .product_config import load_config
from .qualification_fixture import QualificationFixture
from .session import ControlSession


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--kind',choices=['fixture','proxy','workspace-proxy','demo'],required=True)
    parser.add_argument('--path',required=True)
    parser.add_argument('--port', type=int, default=0)
    args=parser.parse_args()
    host, port = '127.0.0.1', args.port
    if args.kind=='fixture':app=QualificationFixture(args.path)
    elif args.kind=='demo':
        from .demo import DemoAPI
        app = DemoAPI()
    else:
        if args.kind == 'workspace-proxy':
            from types import SimpleNamespace
            from .cli import resolved_config
            config = resolved_config(SimpleNamespace(config=args.path))
            address, port_number = config.listen.rsplit(':', 1)
            host, port = address.strip('[]'), int(port_number)
        else:
            config = load_config(args.path)
        app=ControlSession(config)
    class Server(uvicorn.Server):
        async def startup(self,sockets=None):
            await super().startup(sockets)
            if self.started:
                port=self.servers[0].sockets[0].getsockname()[1]
                print(json.dumps(dict(schema_version=1,port=port)),flush=True)
        async def serve(self,sockets=None):
            loop=asyncio.get_running_loop()
            def stop():self.should_exit=True
            def watch():
                # A daemon avoids leaving a blocked default-executor reader on exit.
                try:sys.stdin.readline()
                finally:
                    if not loop.is_closed():loop.call_soon_threadsafe(stop)
            threading.Thread(target=watch,daemon=True).start()
            await super().serve(sockets)
    server=Server(uvicorn.Config(app,host=host,port=port,workers=1,loop='asyncio',
        log_level='error',access_log=False,server_header=False,date_header=False,timeout_graceful_shutdown=10))
    server.run()


if __name__=='__main__':main()

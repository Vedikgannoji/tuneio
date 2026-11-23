"""Private loopback subprocess entry point used only by the benchmark coordinator."""
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
    parser.add_argument('--kind',choices=['fixture','proxy','demo'],required=True)
    parser.add_argument('--path',required=True)
    parser.add_argument('--port', type=int, default=0)
    args=parser.parse_args()
    if args.kind=='fixture':app=QualificationFixture(args.path)
    elif args.kind=='demo':
        from .demo import DemoAPI
        app = DemoAPI()
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
    server=Server(uvicorn.Config(app,host='127.0.0.1',port=args.port,workers=1,loop='asyncio',
        log_level='error',access_log=False,server_header=False,date_header=False,timeout_graceful_shutdown=10))
    server.run()


if __name__=='__main__':main()

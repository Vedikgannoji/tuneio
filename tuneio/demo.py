"""Disposable RAM-only upstream for reviewing the generic proxy's cache semantics."""
import json


class DemoAPI:
    def __init__(self):
        self.product={'id':1,'name':'Cache review product','price_cents':2500}
        self.version=0

    async def __call__(self,scope,receive,send):
        if scope['type']=='lifespan':
            while True:
                message=await receive()
                if message['type']=='lifespan.startup':await send({'type':'lifespan.startup.complete'})
                elif message['type']=='lifespan.shutdown':
                    await send({'type':'lifespan.shutdown.complete'});return
        if scope['type']!='http':return
        path,method=scope['path'],scope['method']
        status,data=404,{'detail':'Not found'}
        public=False
        if method=='GET':
            if path=='/api/products':status,data=200,[dict(self.product)]
            elif path=='/api/products/1':status,data=200,dict(self.product)
            elif path=='/api/reports/catalog':status,data=200,dict(version=self.version,price_cents=self.product['price_cents'])
            public=status==200
        elif method=='PATCH' and path=='/api/products/1':
            body=bytearray()
            while True:
                message=await receive()
                if message['type']=='http.disconnect':return
                body.extend(message.get('body',b''))
                if len(body)>4096:
                    status,data=413,{'detail':'Fixture payload too large'};break
                if not message.get('more_body'):
                    try:
                        payload=json.loads(body)
                        if not isinstance(payload,dict) or set(payload)!={'price_cents'}:raise ValueError()
                        price=payload['price_cents']
                        if type(price) is not int or not 0<=price<=1_000_000_000:raise ValueError()
                    except (ValueError,UnicodeError):
                        status,data=422,{'detail':'Expected an integer price_cents'}
                    else:
                        self.product['price_cents']=price;self.version+=1
                        status,data=200,dict(self.product)
                    break
        encoded=json.dumps(data,separators=(',',':')).encode()
        await send({'type':'http.response.start','status':status,'headers':[(b'content-type',b'application/json'),
                    (b'content-length',str(len(encoded)).encode()),(b'cache-control',b'public, max-age=60' if public else b'no-store')]})
        await send({'type':'http.response.body','body':encoded})



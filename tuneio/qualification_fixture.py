"""Disposable SQL workload for explicit product qualification; never uses a demo DB."""
import asyncio
from contextlib import closing
from functools import lru_cache
import json
from pathlib import Path
import sqlite3


PAYLOAD_BYTES = 1024


def encode(data):
    """Produce the same fixed-size JSON representation for every workload mode."""
    prefix=json.dumps(data,separators=(',',':'),sort_keys=True).encode()
    if len(prefix)>PAYLOAD_BYTES:raise ValueError('Fixture payload exceeds reference size')
    return prefix+b' '*(PAYLOAD_BYTES-len(prefix))


@lru_cache(maxsize=4)
def reference_rows(orders):
    rows=[[category,orders//8+int(category<orders%8),0] for category in range(8)]
    for index in range(orders):rows[index%8][2]+=(index%100+1)*100
    return tuple(tuple(row) for row in rows)


def reference(orders,version=0):
    rows=[list(row) for row in reference_rows(orders)]
    if version:rows[0][2]+=version
    return encode(dict(rows=rows,version=version))


def seed(path,orders):
    # Refuse any existing file: qualification may only create an isolated dataset.
    with Path(path).open('xb'):pass
    with closing(sqlite3.connect(path)) as connection,connection:
        connection.executescript('CREATE TABLE orders(id INTEGER PRIMARY KEY, category INTEGER, amount INTEGER);'
                                 'CREATE TABLE state(version INTEGER); INSERT INTO state VALUES(0);')
        connection.executemany('INSERT INTO orders VALUES(?,?,?)',((i+1,i%8,(i%100+1)*100) for i in range(orders)))


class QualificationFixture:
    def __init__(self,path):
        self.path=Path(path)
        self.reads=self.writes=self.failures=0

    def report(self):
        with closing(sqlite3.connect(self.path)) as connection:
            rows=connection.execute('SELECT category,count(*),sum(amount) FROM orders GROUP BY category ORDER BY category').fetchall()
            version=connection.execute('SELECT version FROM state').fetchone()[0]
        return encode(dict(rows=rows,version=version))

    def mutate(self):
        with closing(sqlite3.connect(self.path)) as connection,connection:
            connection.execute('UPDATE orders SET amount=amount+1 WHERE id=1')
            connection.execute('UPDATE state SET version=version+1')
            version=connection.execute('SELECT version FROM state').fetchone()[0]
        return encode(dict(version=version))

    async def __call__(self,scope,receive,send):
        if scope['type']=='lifespan':
            while True:
                event=await receive()
                await send({'type':event['type']+'.complete'})
                if event['type']=='lifespan.shutdown':return
        if scope['type']!='http':return
        path,method=scope['path'],scope['method']
        status,public,data=404,False,encode({'detail':'Not found'})
        if path=='/health' and method=='GET':status,data=200,encode({'ready':True})
        elif path=='/stats' and method=='GET':
            status,data=200,encode(dict(reads=self.reads,writes=self.writes,failures=self.failures))
        elif path in ('/payload','/report') and method=='GET':
            self.reads+=1
            status,public=200,True
            data=encode({'message':'Reference payload'}) if path=='/payload' else await asyncio.to_thread(self.report)
        elif path=='/mutate' and method=='POST':
            # Bodyless, explicit fixture operation. It only changes this temporary DB.
            self.writes+=1;status,data=200,await asyncio.to_thread(self.mutate)
        elif path=='/fault' and method=='GET':
            self.failures+=1;status,data=503,encode({'detail':'Injected fixture failure'})
        await send({'type':'http.response.start','status':status,'headers':[
            (b'content-type',b'application/json'),(b'content-length',str(len(data)).encode()),
            (b'cache-control',b'public, max-age=60' if public else b'no-store')]})
        await send({'type':'http.response.body','body':data})

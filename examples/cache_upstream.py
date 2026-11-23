"""Compatibility entry point for the packaged disposable RAM demo."""
from tuneio.demo import DemoAPI as CacheUpstream

if __name__=='__main__':
    import uvicorn
    uvicorn.run(CacheUpstream(),host='127.0.0.1',port=3000,access_log=False)

from fastapi import APIRouter
from fastapi.responses import JSONResponse

import remote_limits

router = APIRouter()


@router.get('/api/agent/status')
def remote_status():
    return JSONResponse(remote_limits.status(), headers={'Cache-Control': 'no-store'})

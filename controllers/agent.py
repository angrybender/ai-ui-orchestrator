from fastapi import APIRouter, HTTPException

import agent_launcher

router = APIRouter()


@router.post('/api/agent/run', status_code=202)
def run_agent():
    try:
        pid = agent_launcher.launch()
    except RuntimeError:
        raise HTTPException(status_code=503, detail='Unable to create agent executor process.') from None
    if pid is None:
        raise HTTPException(status_code=409, detail='An agent run is already active.')
    return {'pid': pid}

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import agent_launcher
import agent_store
import board_store

router = APIRouter()


@router.get('/api/agent/recovery')
def recovery_candidates():
    with board_store.connect() as connection:
        rows = connection.execute(
            "SELECT r.id AS run_id, t.task_id FROM agent_runs r LEFT JOIN board_tasks t ON t.id = r.task_pk "
            "WHERE r.agent_uncertain = 1 AND r.state NOT IN ('PREPARING', 'RUNNING') ORDER BY r.created_at, r.id"
        ).fetchall()
    return {'runs': [dict(row) for row in rows]}


class RecoveryPayload(BaseModel):
    run_id: str = Field(min_length=1, max_length=128)


@router.post('/api/agent/recovery')
def recover_agent(payload: RecoveryPayload):
    # The button obtains the operator's attestation for this specific run.
    # Keep the transactional state/history protection in the shared store.
    try:
        agent_store.confirm_remote_stop(payload.run_id)
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    return {'run_id': payload.run_id, 'remaining': len(recovery_candidates()['runs'])}


@router.post('/api/agent/run', status_code=202)
def run_agent():
    try:
        pid = agent_launcher.launch()
    except RuntimeError:
        raise HTTPException(status_code=503, detail='Unable to create agent executor process.') from None
    if pid is None:
        raise HTTPException(status_code=409, detail='An agent run is already active.')
    return {'pid': pid}

from typing import Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from database_store import clear_database

router = APIRouter()


class ClearDatabasePayload(BaseModel):
    confirmed: Literal[True]


@router.post('/api/database/clear')
def clear_local_database(payload: ClearDatabasePayload):
    try:
        clear_database()
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from None
    return {'cleared': True}

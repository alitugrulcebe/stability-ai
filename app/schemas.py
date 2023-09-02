from typing import Optional
from pydantic import BaseModel

class AIRequest(BaseModel):
    email: str
    prompt: str
    webhook_url: str
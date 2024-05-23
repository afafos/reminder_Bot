import schedule
from fastapi import FastAPI
import datetime
from main import bot

app = FastAPI()


@app.post('/message')
async def message():
    schedule.run_pending()

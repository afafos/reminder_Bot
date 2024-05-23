FROM python:3.8-slim-buster

WORKDIR /app
COPY . /app

RUN apt-get update && apt-get install -y gcc
RUN pip install --no-cache-dir -r requirements.txt

EXPOSE 5000

CMD ["python", "./main.py"]

FROM python:3.12-alpine
WORKDIR /app

COPY index.py ./

ENV PYTHONUNBUFFERED=1
ENV PORT=3000
EXPOSE 3000

CMD ["python", "index.py"]

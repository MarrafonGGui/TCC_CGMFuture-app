FROM python:3.11-slim

WORKDIR /app

# Instalar dependências do sistema operacional necessárias
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Copiar arquivo de dependências e instalar
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar código da aplicação
COPY . .

# Expor a porta da aplicação
EXPOSE 8000

# Comando para iniciar o servidor web
CMD ["python", "run.py"]
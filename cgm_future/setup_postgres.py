"""Cria o banco PostgreSQL (se necessário), inicializa as tabelas e carrega a base inicial."""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

try:
    import psycopg2
    from psycopg2 import sql
except ImportError:
    print("Erro: psycopg2-binary não está instalado. Execute: pip install psycopg2-binary")
    sys.exit(1)


def get_pg_config():
    db_url = os.getenv("DATABASE_URL")
    if db_url:
        return {"dsn": db_url}
    return {
        "host": os.getenv("POSTGRES_HOST", "localhost"),
        "port": int(os.getenv("POSTGRES_PORT", 5432)),
        "dbname": os.getenv("POSTGRES_DB", "cgm_db"),
        "user": os.getenv("POSTGRES_USER", "postgres"),
        "password": os.getenv("POSTGRES_PASSWORD", "postgres"),
    }


def create_database_if_not_exists(cfg):
    target_db = cfg.get("dbname")
    if not target_db:
        return

    # Conecta ao banco administrativo padrão 'postgres' para verificar/criar o banco da aplicação
    admin_cfg = dict(cfg)
    admin_cfg["dbname"] = "postgres"

    print(f"Verificando banco '{target_db}' no PostgreSQL...")
    try:
        conn = psycopg2.connect(**admin_cfg)
        conn.autocommit = True
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s;", (target_db,))
        if not cur.fetchone():
            print(f"Criando banco '{target_db}'...")
            cur.execute(sql.SQL("CREATE DATABASE {};").format(sql.Identifier(target_db)))
            print(f"Banco '{target_db}' criado.")
        else:
            print(f"Banco '{target_db}' já existe.")
        cur.close()
        conn.close()
    except Exception as e:
        print(f"Aviso ao verificar banco administrativo: {e}")
        print("Tentando conexão direta ao banco configurado...")


def init_postgres_schema():
    print("Inicializando tabelas e dados iniciais...")
    from app.database import init_postgres, seed_initial_data_if_empty, get_database_stats
    init_postgres()
    seeded = seed_initial_data_if_empty()
    if seeded:
        print("Base inicial importada para o usuário 'adm'.")
    stats = get_database_stats()
    print(f"Total de leituras no PostgreSQL: {stats['total_readings']}")
    print("Configuração concluída.")


if __name__ == "__main__":
    config = get_pg_config()
    print("Configuração do PostgreSQL — Monitoramento CGM")
    if "dsn" not in config:
        print(f"Host: {config['host']}:{config['port']} | Banco: {config['dbname']} | Usuário: {config['user']}")
        create_database_if_not_exists(config)
    try:
        init_postgres_schema()
    except Exception as e:
        print(f"\nErro ao conectar ao PostgreSQL:\n{e}")
        print("\nVerifique se o PostgreSQL está ativo e se as credenciais do .env estão corretas.")
        sys.exit(1)

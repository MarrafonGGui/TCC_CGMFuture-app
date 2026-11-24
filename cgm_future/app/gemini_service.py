from __future__ import annotations
import os
import time
from pathlib import Path
from typing import List, Literal, Optional
from pydantic import BaseModel, Field
from dotenv import load_dotenv, dotenv_values

# Carrega o .env local sem sobrescrever variáveis de rede definidas pelo Docker
BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"
load_dotenv(ENV_PATH)


# Schemas de saída estruturada para retorno JSON do Gemini

class OpcaoRefeicao(BaseModel):
    descricao: str = Field(
        description="Descrição detalhada dos alimentos, porções e itens componentes desta opção de refeição."
    )
    carboidratos_g: float = Field(
        description="Total estimado de carboidratos disponíveis na porção em gramas (g)."
    )
    proteinas_g: float = Field(
        default=0.0,
        description="Total estimado de proteínas em gramas (g)."
    )
    gorduras_g: float = Field(
        default=0.0,
        description="Total estimado de gorduras/lipídios em gramas (g)."
    )
    calorias_kcal: float = Field(
        default=0.0,
        description="Total energético estimado em quilocalorias (kcal)."
    )
    indice_glicemico_estimado: Literal["Baixo", "Médio", "Alto"] = Field(
        description="Classificação estimada do impacto glicêmico/velocidade de absorção (Baixo: IG < 55, Médio: IG 56-69, Alto: IG >= 70)."
    )


class Refeicao(BaseModel):
    horario: str = Field(
        description="Horário planejado ou típico da refeição no formato estrito HH:MM (ex.: '07:30', '12:00', '16:00', '19:30')."
    )
    nome_refeicao: str = Field(
        description="Nome da refeição (ex.: Café da Manhã, Colação, Almoço, Lanche da Tarde, Jantar, Ceia)."
    )
    opcoes: List[OpcaoRefeicao] = Field(
        description="Lista de opções de refeições ou alternativas disponíveis para este horário."
    )


class AnaliseDieta(BaseModel):
    refeicoes: List[Refeicao] = Field(
        description="Lista completa de refeições estruturadas com contagem de carboidratos e macronutrientes decompostos."
    )


# Integração com a API Gemini (SDK google-genai)

def get_gemini_api_key() -> str | None:
    # Lê direto do .env para refletir alterações de chave sem reiniciar o servidor
    file_env = dotenv_values(ENV_PATH) if ENV_PATH.exists() else {}
    key = file_env.get("GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY")
    if key and key.strip() and not key.startswith("sua_chave_"):
        return key.strip()
    return None


DEPRECATED_MODEL_PREFIXES = ("gemini-1.5", "gemini-2.0", "gemini-2.5")

DEFAULT_GEMINI_MODELS = [
    "gemini-3.8-flash",
    "gemini-4-hydrogen",
    "gemini-3.7-flash",
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
]


def get_gemini_model_candidates() -> list[str]:
    """Prioriza GEMINI_MODEL do .env e mantém os demais modelos ativos como fallback."""
    file_env = dotenv_values(ENV_PATH) if ENV_PATH.exists() else {}
    preferred = str(file_env.get("GEMINI_MODEL") or os.getenv("GEMINI_MODEL") or "").strip()
    if preferred.startswith("models/"):
        preferred = preferred.split("/", 1)[1]
    if preferred.startswith(DEPRECATED_MODEL_PREFIXES):
        preferred = "gemini-3.8-flash"
    models = [preferred] if preferred else []
    models += [m for m in DEFAULT_GEMINI_MODELS if m != preferred]
    return models


def get_demo_diet_plan() -> AnaliseDieta:
    """Plano alimentar de referência usado quando não há chave configurada ou em contingência."""
    return AnaliseDieta(
        refeicoes=[
            Refeicao(
                horario="07:30",
                nome_refeicao="Café da Manhã",
                opcoes=[
                    OpcaoRefeicao(
                        descricao="2 fatias de pão integral 100% (50g) com queijo minas frescal (30g) e 1 xícara de café com leite desnatado (200ml) sem açúcar",
                        carboidratos_g=28.5,
                        proteinas_g=14.0,
                        gorduras_g=6.2,
                        calorias_kcal=226.0,
                        indice_glicemico_estimado="Médio"
                    ),
                    OpcaoRefeicao(
                        descricao="Omelete de 2 ovos com espinafre e tomate + 1 fatia de mamão papaia (100g) com 1 colher de sopa de farelo de aveia",
                        carboidratos_g=16.0,
                        proteinas_g=16.5,
                        gorduras_g=11.0,
                        calorias_kcal=230.0,
                        indice_glicemico_estimado="Baixo"
                    )
                ]
            ),
            Refeicao(
                horario="12:30",
                nome_refeicao="Almoço",
                opcoes=[
                    OpcaoRefeicao(
                        descricao="4 colheres de sopa de arroz integral (100g), 1 concha média de feijão carioca (100g), 1 filé de peito de frango grelhado (120g) e salada de alface e pepino com azeite",
                        carboidratos_g=42.0,
                        proteinas_g=36.0,
                        gorduras_g=8.5,
                        calorias_kcal=388.0,
                        indice_glicemico_estimado="Médio"
                    ),
                    OpcaoRefeicao(
                        descricao="1 prato de macarrão penne integral (140g cozido) ao molho de tomate caseiro com carne moída magra (patinho 100g) e salada verde",
                        carboidratos_g=48.0,
                        proteinas_g=31.0,
                        gorduras_g=7.0,
                        calorias_kcal=379.0,
                        indice_glicemico_estimado="Médio"
                    )
                ]
            ),
            Refeicao(
                horario="16:00",
                nome_refeicao="Lanche da Tarde",
                opcoes=[
                    OpcaoRefeicao(
                        descricao="1 maçã média com casca (120g) e 1 punhado de castanhas de caju (20g)",
                        carboidratos_g=19.0,
                        proteinas_g=4.2,
                        gorduras_g=9.5,
                        calorias_kcal=178.0,
                        indice_glicemico_estimado="Baixo"
                    ),
                    OpcaoRefeicao(
                        descricao="1 pote de iogurte natural desnatado (160g) com 1 colher de chia e morangos frescos (80g)",
                        carboidratos_g=14.0,
                        proteinas_g=9.0,
                        gorduras_g=4.0,
                        calorias_kcal=128.0,
                        indice_glicemico_estimado="Baixo"
                    )
                ]
            ),
            Refeicao(
                horario="19:30",
                nome_refeicao="Jantar",
                opcoes=[
                    OpcaoRefeicao(
                        descricao="1 filé de salmão grelhado (120g), 1 batata doce média assada (120g) e brócolis ao vapor regado com 1 colher de sobremesa de azeite",
                        carboidratos_g=32.0,
                        proteinas_g=28.0,
                        gorduras_g=14.0,
                        calorias_kcal=366.0,
                        indice_glicemico_estimado="Médio"
                    ),
                    OpcaoRefeicao(
                        descricao="Sopa de legumes (abobrinha, chuchu, cenoura) com frango desfiado (100g) e 1 torrada integral",
                        carboidratos_g=22.0,
                        proteinas_g=24.0,
                        gorduras_g=5.0,
                        calorias_kcal=229.0,
                        indice_glicemico_estimado="Baixo"
                    )
                ]
            ),
            Refeicao(
                horario="22:00",
                nome_refeicao="Ceia",
                opcoes=[
                    OpcaoRefeicao(
                        descricao="1 xícara de chá de camomila sem açúcar e 2 nozes chilenas",
                        carboidratos_g=2.5,
                        proteinas_g=1.5,
                        gorduras_g=6.5,
                        calorias_kcal=74.0,
                        indice_glicemico_estimado="Baixo"
                    )
                ]
            )
        ]
    )


def analyze_diet_content(
    text: Optional[str] = None,
    file_bytes: Optional[bytes] = None,
    mime_type: Optional[str] = None,
    filename: Optional[str] = None
) -> dict:
    """Extrai refeições, macronutrientes e índice glicêmico de texto ou arquivo via Gemini."""
    api_key = get_gemini_api_key()

    has_text = bool(text and text.strip())
    has_file = bool(file_bytes and len(file_bytes) > 0)

    if not has_text and not has_file:
        raise ValueError("Nenhum conteúdo fornecido. Digite a dieta ou anexe um arquivo PDF, imagem ou TXT.")

    if not api_key:
        demo = get_demo_diet_plan()
        return {
            "status": "warning",
            "message": "GEMINI_API_KEY não configurada no arquivo .env. Exibindo plano alimentar de demonstração.",
            "is_demo": True,
            "data": demo.model_dump()
        }

    try:
        try:
            from google import genai
            from google.genai import types
        except ModuleNotFoundError as imp_err:
            raise RuntimeError(
                "Dependência 'google-genai' não encontrada neste ambiente "
                f"({imp_err}). Instale com 'pip install -r requirements.txt' ou recrie o container com 'docker compose up -d --build'."
            ) from imp_err

        client = genai.Client(api_key=api_key)

        prompt_instruction = (
            "Você é um Endocrinologista e Nutricionista Clínico especializado em "
            "Diabetologia e Contagem de Carboidratos. "
            "Analise o plano alimentar ou cardápio enviado (texto, imagem ou PDF) e extraia todas as refeições. "
            "Para cada refeição: "
            "1. Defina o horário no formato HH:MM (se não estiver explícito, use um horário padrão coerente). "
            "2. Informe o nome da refeição (Café da Manhã, Almoço, etc.). "
            "3. Separe cada opção de prato com descrição, carboidratos líquidos (carboidratos_g), "
            "proteínas (proteinas_g), gorduras (gorduras_g), calorias (calorias_kcal) "
            "e o índice glicêmico estimado ('Baixo', 'Médio' ou 'Alto'). "
            "Responda estritamente no formato JSON do schema."
        )

        contents = []

        if has_file and file_bytes:
            detected_mime = mime_type or "application/octet-stream"
            if filename:
                ext = Path(filename).suffix.lower()
                if ext == ".pdf":
                    detected_mime = "application/pdf"
                elif ext in (".png", ".webp"):
                    detected_mime = f"image/{ext[1:]}"
                elif ext in (".jpg", ".jpeg"):
                    detected_mime = "image/jpeg"
                elif ext in (".txt", ".md", ".csv"):
                    detected_mime = "text/plain"

            if detected_mime.startswith("text/"):
                decoded_text = file_bytes.decode("utf-8", errors="replace")
                contents.append(f"Conteúdo do arquivo ({filename or 'texto'}):\n{decoded_text}")
            else:
                part = types.Part.from_bytes(data=file_bytes, mime_type=detected_mime)
                contents.append(part)

        if has_text:
            contents.append(f"Descrição da dieta pelo usuário:\n{text}")

        contents.append(prompt_instruction)

        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_json_schema=AnaliseDieta.model_json_schema(),
        )

        # Percorre a fila de modelos caso algum esteja temporariamente sobrecarregado (503/429)
        response = None
        last_error: Exception | None = None
        transient_error: Exception | None = None
        for model_name in get_gemini_model_candidates():
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=contents,
                    config=config,
                )
                if response:
                    break
            except Exception as model_err:
                last_error = model_err
                msg = str(model_err)
                is_transient = any(k in msg for k in [
                    "503", "UNAVAILABLE", "high demand", "overloaded",
                    "429", "RESOURCE_EXHAUSTED", "quota",
                ])
                is_not_found = any(k in msg for k in [
                    "NOT_FOUND", "404", "no longer available",
                ])
                if is_transient:
                    transient_error = model_err
                    print(f"[Gemini] Modelo '{model_name}' sob alta demanda. Tentando próximo modelo...")
                    time.sleep(0.8)
                    continue
                if is_not_found:
                    print(f"[Gemini] Modelo '{model_name}' indisponível. Tentando próximo modelo...")
                    continue
                raise

        if response is None:
            raise transient_error or last_error or RuntimeError("Nenhum modelo Gemini respondeu no momento.")

        # O SDK pode devolver response.parsed como dict ou instância Pydantic dependendo do config
        parsed_obj = getattr(response, "parsed", None)
        if isinstance(parsed_obj, AnaliseDieta):
            structured_data = parsed_obj
        elif isinstance(parsed_obj, dict):
            structured_data = AnaliseDieta.model_validate(parsed_obj)
        elif response.text:
            structured_data = AnaliseDieta.model_validate_json(response.text)
        else:
            raise RuntimeError("O Gemini retornou uma resposta vazia.")

        return {
            "status": "ok",
            "message": "Dieta analisada com sucesso pelo Google Gemini.",
            "is_demo": False,
            "data": structured_data.model_dump()
        }

    except Exception as e:
        error_msg = str(e)
        # Em caso de cota esgotada ou indisponibilidade geral dos servidores, ativa o plano de contingência
        is_contingency = any(k in error_msg.lower() for k in [
            "api_key_invalid", "quota", "permission", "503", "unavailable", "high demand", "resource_exhausted"
        ])
        if is_contingency:
            demo = get_demo_diet_plan()
            return {
                "status": "warning",
                "message": f"Serviço Gemini temporariamente indisponível ({error_msg}). Exibindo plano alimentar de referência.",
                "is_demo": True,
                "data": demo.model_dump()
            }
        raise RuntimeError(f"Erro ao processar análise nutricional com Gemini: {error_msg}")

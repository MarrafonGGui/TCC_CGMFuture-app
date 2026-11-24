# TCC — CGM + IA

Protótipo em Python/FastAPI para analisar exportações CSV do FreeStyle Libre, calcular métricas glicêmicas e testar previsão de glicose 30 minutos à frente.

## Correção metodológica desta versão
O CSV analisado cobre vários períodos de uso do sensor ao longo de aproximadamente 646 dias. A versão v3 **não trata esse intervalo como uma única série contínua**. Ela:

1. separa automaticamente períodos de uso quando há lacuna superior a 2 horas;
2. calcula um resumo por período;
3. reamostra para 15 minutos apenas dentro de cada período;
4. permite interpolação somente de pequenos buracos (até 30 minutos);
5. não cria dados entre períodos sem registros;
6. constrói as janelas de 30 min de histórico → 30 min de previsão somente dentro do mesmo período;
7. usa divisão cronológica 70/30 para avaliação, evitando `train_test_split` aleatório.

## CSV do Libre
A leitura considera a primeira linha como metadado e a segunda como cabeçalho (`skiprows=1`), usando:
- `Carimbo de data/hora do dispositivo`
- `Histórico de glicose mg/dL`

## Modelos
- Regressão Linear (baseline)
- Random Forest
- Gradient Boosting

Métricas: MAE, RMSE e R².

## Dados sintéticos
O endpoint de dados sintéticos continua disponível para desenvolvimento. Eles devem ser tratados no TCC explicitamente como **dados sintéticos**, não como dados clínicos reais.

## Execução
```bash
python -m venv .venv
.venv\\Scripts\\activate
pip install -r requirements.txt
python run.py
```
Acesse `http://127.0.0.1:8000`.

> Este protótipo é uma ferramenta acadêmica de análise/predição e não substitui diagnóstico, confirmação laboratorial, glicemia capilar ou acompanhamento profissional. Não realiza ajuste de insulina.

## Persistência em Banco de Dados PostgreSQL e Previsão Colaborativa

A partir desta versão, a aplicação suporta **PostgreSQL** como banco de dados principal de produção:

1. **Estrutura de Tabelas no PostgreSQL**:
   - `glucose_readings`: registros com `id SERIAL`, `timestamp TIMESTAMP WITHOUT TIME ZONE UNIQUE`, `glucose NUMERIC(5,1)` e `source VARCHAR(100)` (`libre_csv`, `manual`, `sintetico`).
   - `sources`: rastreamento de arquivos e fontes importadas.
   - `predictions_log`: histórico de previsões geradas, modelos utilizados e validação contra leituras reais subsequentes.
   - `settings`: pares chave-valor para parâmetros da aplicação.

2. **Configuração via Arquivo `.env`**:
   O arquivo `.env` na raiz do projeto define as credenciais:
   ```env
   DB_TYPE=postgres
   POSTGRES_HOST=localhost
   POSTGRES_PORT=5432
   POSTGRES_DB=cgm_db
   POSTGRES_USER=postgres
   POSTGRES_PASSWORD=postgres
   
   # Ou via string de conexão completa (Supabase, Neon, Render, AWS RDS):
   # DATABASE_URL=postgresql://usuario:senha@host:5432/cgm_db
   ```

3. **Provisionamento Rápido do PostgreSQL**:
   Execute o script utilitário para criar o banco de dados e migrar os dados iniciais:
   ```bash
   python setup_postgres.py
   ```
   *(Caso queira rodar o PostgreSQL via Docker: `docker run --name cgm-postgres -e POSTGRES_PASSWORD=postgres -e POSTGRES_DB=cgm_db -p 5432:5432 -d postgres:16`)*

4. **Modo de Contingência Seguro**:
   Caso o PostgreSQL não esteja acessível no momento, o sistema faz o fallback automático para o SQLite local (`cgm.db`), mantendo a aplicação 100% funcional. Assim que o PostgreSQL estiver online, basta clicar no botão **"Reconectar PostgreSQL"** na interface.

## Sistema Multi-Usuário com Autenticação e Aprendizado Colaborativo

A aplicação conta com arquitetura **Multi-Usuário** completa com isolamento de dados:

1. **Tela de Login e Controle de Sessão**:
   - Tela de login na abertura do sistema com proteção de rotas e sessões via cookies HTTP-Only seguros.
   - **Credenciais padrão do Administrador**:
     - Usuário: `adm`
     - Senha: `adm`

2. **Perfil Administrador (`adm`)**:
   - O usuário `adm` possui acesso ao **Painel do Administrador** para criar novos usuários e pacientes.
   - Pode definir nome de login, nome completo, senha e perfil (Usuário Padrão ou Administrador).
   - Visualiza a lista de usuários, quantidade de leituras gravadas e previsões de cada um.

3. **Isolamento de Dados no Banco de Dados (PostgreSQL e SQLite)**:
   - As tabelas `glucose_readings`, `sources` e `predictions_log` são vinculadas à chave estrangeira `user_id`.
   - Cada usuário só tem acesso e visualiza as suas próprias leituras de sensor, entradas manuais, gráficos diários de 24h, métricas clínicas (TIR, TAR, TBR, GMI) e logs de predições.
   - Restrição de unicidade por usuário e carimbo de data/hora: `UNIQUE(user_id, timestamp)`.

4. **Modelo de IA Colaborativo vs. Inferência Individual**:
   - **Treinamento Global / Populacional**: Os modelos preditivos supervisionados (Regressão Linear, Random Forest e Gradient Boosting) aprendem a partir da base agregada de **todos os usuários**, respeitando a segmentação de sessões individuais de cada paciente.
   - **Inferência Personalizada**: No momento de prever (+15 min e +30 min), o sistema considera estritamente os dados e variáveis temporais (lags e taxa de variação) **do usuário que está logado** e realizou a requisição.




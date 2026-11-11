# VagasUnB (Extraordinária)

Gratuito e independente da UnB: catálogo público local e monitoramento de vagas. Não realiza, reserva nem verifica matrícula. Alertas não garantem disponibilidade. Cliente público adaptado do Followw UnB (MIT), com aviso preservado em `THIRD_PARTY_NOTICES`. Referências locais não são dependências.

A instalação padrão usa **demo offline**: catálogo e destinatários sintéticos, sem chamadas ao SIGAA ou Telegram. Não exige bot, token ou credenciais acadêmicas.

## Preparar e executar

Windows: PowerShell 5.1+, [uv](https://docs.astral.sh/uv/) e Docker Desktop com Linux containers. O setup não instala ferramentas do sistema. Abra PowerShell na raiz:

```powershell
git clone https://github.com/xGabrielCv/VagasUnB-Extraordinaria.git
cd VagasUnB-Extraordinaria
.\scripts\setup.ps1
.\scripts\start.ps1 -NoBrowser
.\scripts\status.ps1
.\scripts\open-dashboard.ps1
.\scripts\restart.ps1 -NoBrowser
.\scripts\stop.ps1
.\scripts\reset-demo.ps1
```

O setup pede senha administrativa própria (mínimo 12 caracteres), guardada como hash Argon2. Painel: **http://127.0.0.1:8765**; só está ativo após healthcheck bem-sucedido. Simulador em `/demo`, após login. Não há senha padrão. Restart preserva dados/chaves; reset exige confirmação e recusa modo real. Stop só afeta esta instância, sem remover volumes.

Alternativa nativa: Python 3.12 gerenciado pelo uv e binários PostgreSQL 17 já disponíveis, sem serviço Windows. O banco é processo separado, em loopback na porta 55437:

```powershell
.\scripts\setup.ps1 -NativePgBin 'C:\caminho\postgresql\bin'
.\scripts\start.ps1 -NoBrowser
```

Configuração privada em `.private/`; nunca publique essa pasta. Trocar senha no modo nativo: `.\.venv\Scripts\python -m vagasunb.cli admin`. Compose: `docker compose run --rm admin python -m vagasunb.cli admin`.

Linux: `sh scripts/linux.sh setup`, `sh scripts/linux.sh start`, `sh scripts/linux.sh status`, `sh scripts/linux.sh stop`. Imagem preparada para amd64/arm64 sem plataforma fixada; valide build e dependências na arquitetura de destino. Migração é serviço único, anterior a admin/worker. PostgreSQL não publica porta. Limites de recursos em `.env.example`.

## Experimentar

Selecione um destinatário sintético e envie `/start`. Busque `DEMO0001 01`, abra detalhes, escolha uma/várias turmas ou disciplina inteira e confirme. Há 12 disciplinas sintéticas com duas turmas cada. Pausadas contam no limite de dez disciplinas.

Altere 0 → 1 vaga. Uma **nova observação válida** depois do debounce de 10 s confirma a abertura; o relógio sozinho não confirma. Aguarde agrupamento de 3 s e use “Despachar agora”, ou aguarde o worker. Alertas simulados também aparecem na conversa. Experimente continuar, informar que conseguiu vaga, encerrar, pausar ou `/apagar_dados`. Após excluir, `/menu` não recria cadastro; somente `/start`.

Painel: dados agregados, catálogo, coletas, outbox, recursos do processo/banco, registros sanitizados e configuração. Coleta/despacho podem ser pausados independentemente. Pedidos de coleta se limitam aos escopos configurados. Containers são controlados pelos scripts; não há shell, SQL livre ou Docker socket no painel.

## Privacidade e operação

Só o chat ID externo é persistido, cifrado com AES-256-GCM e nonce aleatório, com índice HMAC-SHA-256 de chave independente. Inscrições, preferências e controle mínimo usam ID interno aleatório. Não persistimos perfil, nome do estudante, username, contato, conversa, busca textual ou update SDK. São dados pseudonimizados; não anonimato absoluto. O Telegram processa seus próprios dados. Docentes públicos integram apenas o catálogo.

Chaves ficam fora da imagem/banco/Git, em arquivos de runtime. **Não gere chaves novas para banco existente.** Preserve os segredos ao preservar o volume. Perda de chave impede descriptografia; chave incompatível bloqueia operações. Compose secrets não cifra o arquivo no host.

Callbacks opacos expiram em 15 min, vinculam destinatário e são de uso único. Exclusão espera eventual envio em andamento e impede novos envios após confirmar; não recolhe mensagens entregues. Claim da outbox é persistente, com locks PostgreSQL. Lease expirada vira **uncertain**, sem retry automático: não há promessa de exactly-once entre Telegram e banco. Retry seguro é limitado e revalida inscrição, estado, período, frescura, origem e cooldown.

Retenção padrão: callbacks 15 min; outbox finalizada 7 dias; eventos/coletas 30 dias; agregados diários 90 dias; destinatário inativo sem inscrição 30 dias. Limpeza roda no worker. Bind em loopback. Acesso remoto requer túnel privado ou HTTPS deliberado e `VAGAS_HTTPS=1` para cookie Secure. Não exponha a demo à internet.

## Exportar e restaurar

Sem backup automático/serviço externo. Exportação usa pg_dump consistente e pacote AES-GCM com manifesto/checksum, sem chaves/token. Transporte `backup_key` **separadamente**, assim como `keys.json`, necessário para chat IDs. Até três pacotes, sete dias e 100 MiB no diretório administrado; pacote maior falha explicitamente.

```powershell
.\scripts\export-instance.ps1
```

Escritores permanecem parados após exportar. Retome com `start.ps1`. Linux/Compose: `sh scripts/linux.sh export`. No destino, crie banco **novo e vazio**, sem executar migrações antes de restaurar. Configure DSN/mode e as mesmas chaves. Não copie volume PostgreSQL bruto entre versões/arquiteturas.

```sh
python -m vagasunb.cli restore --package /caminho/privado/pacote.enc --key /caminho/separado/backup_key
python -m vagasunb.cli diagnose
```

pg_dump/pg_restore no PATH ou `VAGAS_PG_BIN`, preferencialmente na mesma versão maior do servidor. Restauração cancela pendentes, revoga sessões e entra em quarentena, com coleta/despacho pausados. Backups antigos não incorporam exclusões posteriores: concilie-as antes de `python -m vagasunb.cli release-quarantine --confirm`. Depois retome tarefas no painel. Valide contagens/descriptografia e mantenha só uma implantação ativa.

Rotação: pare escritores, preserve chaves antigas privadamente e prepare outro diretório com `keys.json` versionado e novas chaves aleatórias independentes. Execute `python -m vagasunb.cli rotate-keys --new-keys /diretorio/novo`. Após sucesso, configure `VAGAS_SECRETS` para esse diretório antes de reiniciar. A operação transaciona todos os destinatários, mantendo IDs internos.

## Modos futuros

`public` exige ativação explícita (`VAGAS_MODE=public`), DSN próprio sem sufixo `_demo`, segredos próprios e configuração de período/janela/IDs de unidades no painel. Não há catálogo real embutido. O cliente implementa home anônima, formulário/ViewState, renovação, sessões isoladas, limite global e preservação em falhas. Campus público começa desconhecido; não inferimos por prefixo.

`scripts/configure-telegram.ps1 -SaveOnly` grava token secretamente e **não conecta**. Implantação futura exige banco/modo `telegram`, token montado e `VAGAS_TELEGRAM_ENABLE=1` explícito para bot e worker. O perfil `telegram-future` da demo recusa conexão por permanecer em demo. Prepare configuração Compose própria, rede de saída e nome/volume distintos antes de ativar. Um receptor por banco é protegido por lock; não execute o mesmo token em outras implantações. Comandos são preparados localmente, sem registro remoto automático. Nenhuma Oracle é acessada pelos scripts.

## Verificar

```powershell
.\scripts\test.ps1
```

Exige PostgreSQL acessível e permissão de criação de bancos. Testes criam/removem apenas bancos aleatórios `vagas_test_*_demo`/`vagas_restore_*_demo`, aplicam migrações reais, simulam HTTP/Telegram e bloqueiam sockets externos nos testes Python. Não use DSN de produção. `DATABASE_URL` e `VAGAS_SECRETS` permitem ambiente isolado; nunca os imprima. Formatação: `uv run ruff format --check src tests migrations`.

Suporte: [Issues](https://github.com/xGabrielCv/VagasUnB-Extraordinaria/issues), sem dados privados. Fontes técnicas: [PTB](https://docs.python-telegram-bot.org/), [Telegram API](https://core.telegram.org/bots/api), [locks PostgreSQL](https://www.postgresql.org/docs/current/explicit-locking.html), [Compose secrets](https://docs.docker.com/compose/how-tos/use-secrets/).

# NeoVids — manual de instalação, operação e validação

Este documento descreve uma instalação limpa em **Ubuntu 24.04 LTS** para o
ambiente local do NeoVids. O escopo é desenvolvimento/produção local em uma
única máquina: Docker Compose, cinco serviços, PostgreSQL/Redis internos,
Qwen3-TTS em uma GPU NVIDIA e LiteLLM externo.

O diretório recomendado é `/home/$USER/workspaces/neovids`. Não coloque o
workspace em `/mnt/c`, em um disco de gameplay ou em uma pasta sincronizada:
Docker, PostgreSQL e arquivos de mídia têm desempenho e permissões melhores no
filesystem Linux.

## 1. Requisitos e layout

Recomendado:

- Ubuntu 24.04 LTS 64-bit, atualizado;
- 8 GB de RAM é o mínimo operacional, 16 GB ou mais é preferível;
- GPU NVIDIA com VRAM suficiente para o modelo Qwen configurado (a máquina
  validada possui RTX 2060 12 GB);
- pelo menos 100 GB livres para imagens/cache, além do espaço dos vídeos;
- acesso à internet durante a instalação e o primeiro download dos modelos;
- credenciais válidas do LiteLLM para embeddings e alinhamento quando essas
  funções forem usadas.

Layout final:

```text
/home/SEU_USUARIO/workspaces/neovids/
├── ideas-hub/
├── media-indexer/
├── orchestrator/
├── qwen3-tts-workshop/
├── video-composer/
└── neovids-infra/          # CLI, overrides e documentação operacional
```

Dados persistentes ficam dentro dos repositórios (bind mounts) ou em volumes
Docker nomeados. **Nunca remova volumes para “reinstalar”.**

## 2. Ubuntu, Git e utilitários

Após instalar o Ubuntu:

```bash
sudo apt update
sudo apt full-upgrade -y
sudo apt install -y git curl ca-certificates gnupg jq unzip ffmpeg \
  build-essential python3 python3-venv python3-pip
sudo timedatectl set-timezone America/Sao_Paulo
mkdir -p ~/workspaces/neovids
```

`ffmpeg`/`ffprobe` são úteis para smoke tests locais; os containers também
contêm suas próprias ferramentas.

## 3. Driver NVIDIA e suporte Docker à GPU

Em uma máquina NVIDIA, instale o driver recomendado e reinicie:

```bash
ubuntu-drivers devices
sudo ubuntu-drivers autoinstall
sudo reboot
```

Valide depois do reboot:

```bash
nvidia-smi
```

Instale o toolkit NVIDIA para containers seguindo o repositório oficial:

```bash
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | \
  sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | \
  sed 's#^deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | \
  sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt update
sudo apt install -y nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker
sudo systemctl restart docker
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu24.04 nvidia-smi
```

Se o último comando falhar, pare aqui: Qwen não ficará operacional até o
driver/runtime serem corrigidos. Não altere `DEVICE`, `DTYPE` ou o modelo para
contornar uma instalação incompleta.

## 4. Docker Engine e Compose

Remova pacotes conflitantes e instale o Docker pelo repositório oficial:

```bash
for p in docker.io docker-doc docker-compose podman-docker containerd runc; do
  sudo apt remove -y "$p" 2>/dev/null || true
done
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | \
  sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
sudo chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" | \
  sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
newgrp docker
docker version
docker compose version
```

Se o Ubuntu estiver sendo executado dentro de WSL2, mantenha Docker Desktop ou
Docker Engine configurado de forma consistente; não instale duas daemons
concorrentes. Para uma instalação Ubuntu nativa, o `systemctl` acima é o fluxo
esperado.

## 5. Clonagem dos repositórios

```bash
cd ~/workspaces/neovids
git clone git@github.com:bernardoadca18/ideas-hub.git
git clone git@github.com:bernardoadca18/media-indexer.git
git clone git@github.com:bernardoadca18/orchestrator.git
git clone git@github.com:bernardoadca18/qwen3-tts-workshop.git
git clone git@github.com:bernardoadca18/video-composer.git
git clone git@github.com:bernardoadca18/neovids-infra.git
```

Se SSH ainda não estiver configurado, use HTTPS temporariamente ou configure
uma chave GitHub. Não copie um `.env` de outra máquina sem revisar segredos.

Crie os caminhos de conveniência, que apontam para o repositório operacional:

```bash
cd ~/workspaces/neovids
ln -s neovids-infra/scripts scripts
ln -s neovids-infra/infra infra
```

## 6. Configuração dos serviços

Cada serviço possui `.env.example`. Copie-o para `.env` no respectivo
repositório e preencha somente os segredos/URLs necessários:

```bash
cd ~/workspaces/neovids
cp ideas-hub/.env.example ideas-hub/.env
cp media-indexer/.env.example media-indexer/.env
cp orchestrator/.env.example orchestrator/.env
cp qwen3-tts-workshop/.env.example qwen3-tts-workshop/.env
cp video-composer/.env.example video-composer/.env
cp neovids-infra/infra/.env.example neovids-infra/infra/.env
chmod 600 */.env
```

Valores operacionais importantes:

| Serviço | Porta | Observação |
|---|---:|---|
| Ideas Hub API/frontend | 8000/5173 | banco `ideas-hub-db-data` |
| Media Indexer API | 8012 | PostgreSQL 5435; storage local; Whisper lazy |
| Orchestrator API/frontend | 8011/5174 | PostgreSQL 5434; Redis 6380 |
| Qwen Voice | 8889 | GPU, jobs SQLite e outputs persistentes |
| Video Composer | 8013 | SQLite/outputs no volume nomeado |
| LiteLLM externo | 4000 | não é gerenciado pelo NeoVids |

No Media Indexer, configure `LITELLM_BASE_URL` alcançável pelo container e uma
`LITELLM_API_KEY` válida. No Qwen, preserve o `MODEL_REVISION` pinado, o
`DEFAULT_VOICE_PROFILE` e as referências em `data/references`. Não coloque
chaves em Git, logs ou imagens.

No arquivo operacional:

```bash
sed -i "s#^NEOVIDS_BACKUP_ROOT=.*#NEOVIDS_BACKUP_ROOT=$HOME/backups/neovids#" \
  neovids-infra/infra/.env
mkdir -p "$HOME/backups/neovids"
chmod 700 "$HOME/backups/neovids"
```

Os backups são criados apenas quando há migração pendente; uma inicialização
normal já no head não cria dump redundante.

## 7. Primeiro diagnóstico e inicialização

Execute sempre a partir da raiz:

```bash
cd ~/workspaces/neovids
./scripts/neovids doctor
```

Em uma instalação limpa o diagnóstico deve indicar `FRESH`. Faça a primeira
subida com rebuild explícito:

```bash
./scripts/neovids up --build
```

O CLI cria a rede externa `neovids`, os cinco volumes canônicos, aplica
migrations e inicia databases, APIs, workers e frontends em ordem. Acompanhe:

```bash
./scripts/neovids status
./scripts/neovids health
./scripts/neovids logs qwen
```

Depois da primeira instalação, `./scripts/neovids up` basta para iniciar
imagens já construídas. Use `--build` após mudanças de código, dependências ou
Dockerfiles.

## 8. Smoke mínimo obrigatório

```bash
curl --fail http://127.0.0.1:8000/health/ready
curl --fail http://127.0.0.1:8012/health/ready
curl --fail http://127.0.0.1:8011/health/ready
curl --fail http://127.0.0.1:8889/health/ready
curl --fail http://127.0.0.1:8013/health/ready
curl --fail http://127.0.0.1:4000/health/readiness   # externo, se configurado
```

Abra `http://localhost:5173` e `http://localhost:5174` para os frontends. O
`doctor`/`health` pode informar Ollama indisponível; isso é opcional. LiteLLM
é obrigatório apenas para os fluxos que geram embeddings/alinhamento.

Para um teste funcional controlado, use os scripts de smoke documentados nos
repositórios (ASR/visual/temporal/candidate/alignment e o smoke Stage 17 do
Orchestrator). Não use mídia de gameplay real antes de confirmar storage,
volumes e espaço em disco.

## 9. Operação diária e encerramento seguro

```bash
./scripts/neovids status
./scripts/neovids health
./scripts/neovids logs media-indexer
./scripts/neovids restart qwen
```

Para parar tudo sem apagar dados:

```bash
./scripts/neovids down
docker ps --format '{{.Names}}' | grep -E 'ideas|media|orchestrator|qwen|video' || true
docker volume ls --format '{{.Name}}' | grep -E 'ideas-hub|media-indexer|orchestrator|video-composer'
```

Nunca use `docker compose down -v`, `docker volume prune`, `docker system
prune` ou `rm -rf` nos diretórios de dados. O restart do Docker/Ubuntu não deve
ressuscitar serviços NeoVids depois de `down`.

## 10. Mídia, backups e capacidade

O Media Indexer guarda fontes/derivados em `media-indexer/storage`; o Qwen usa
`data/jobs`, `data/outputs` e `data/references`; o Orchestrator guarda áudio em
`data/audio`. Faça backup desses diretórios e dos volumes PostgreSQL conforme a
política do projeto antes de trocar disco/OS. Não monte a unidade de footage no
Compose: selecione fontes explicitamente pela API.

Monitore uma máquina com pouca RAM durante Qwen + Whisper + Composer:

```bash
free -h; swapon --show; nvidia-smi; docker stats --no-stream
df -h ~/workspaces/neovids
```

Evite executar geração Qwen, transcrição e renderização pesada concorrentes em
outras aplicações GPU. O worker Qwen é serial por desenho.

## 11. Validação de desenvolvimento

Após clonar ou atualizar um serviço, execute seus testes no diretório indicado:

```bash
cd ~/workspaces/neovids/media-indexer/backend
pytest
ruff check app tests
mypy app
alembic upgrade head

cd ~/workspaces/neovids/ideas-hub/backend && pytest
cd ~/workspaces/neovids/orchestrator/backend && pytest
cd ~/workspaces/neovids/qwen3-tts-workshop && pytest
cd ~/workspaces/neovids/video-composer && npm ci && npm test
```

Os testes normais usam providers fakes e não baixam Qwen/Whisper. Smokes de GPU,
LiteLLM, pgvector e mídia real são opt-in e devem ser reportados separadamente.

## 12. Troubleshooting

**`doctor` indica PARTIAL / AMBIGUOUS.** Não crie volumes substitutos. Verifique
o nome exato dos cinco volumes canônicos e restaure o volume ausente a partir do
backup.

**Readiness falha.** Rode `status`, depois `logs <service>`. Confirme portas,
permissões dos binds, espaço em disco e `docker info`. Não contorne migrations
ou edite linhas de banco manualmente.

**Qwen não fica pronto.** Valide `nvidia-smi`, `docker run --gpus all ...`,
referência WAV/TXT e o cache Hugging Face. O modelo é carregado no lifespan;
importar o código não o baixa.

**LiteLLM retorna 401/timeout.** Corrija a credencial/endpoint no ambiente
externo. Não remova autenticação nem substitua o provider; os testes locais
devem usar fakes.

**Pouca RAM/swap.** Pare jobs, mantenha apenas um worker GPU e aumente memória
física/swap do Ubuntu. Não altere precisão/modelo como primeira medida.

**Código alterado não aparece no container.** Use `./scripts/neovids up --build`.
O `up` sem `--build` deliberadamente não adivinha se uma imagem está obsoleta.

## 13. Atualizações e rollback operacional

```bash
cd ~/workspaces/neovids
for repo in ideas-hub media-indexer orchestrator qwen3-tts-workshop video-composer neovids-infra; do
  git -C "$repo" status --short
  git -C "$repo" pull --ff-only
done
./scripts/neovids doctor
./scripts/neovids up --build
./scripts/neovids health
```

Leia o changelog/diff antes de migrations. O CLI faz backup quando detecta
migração pendente; downgrade é uma operação deliberada e deve ser executada no
repositório do serviço, nunca apagando o volume.

## 14. Limites arquiteturais

Este ambiente é uma implantação local confiável, de uma réplica. TLS,
autenticação pública, rate limiting, múltiplos workers GPU, brokers e
orquestração Kubernetes não fazem parte desta instalação. Ideas Hub mantém a
intenção editorial; Media Indexer mantém evidências; Orchestrator coordena;
Qwen sintetiza; Video Composer renderiza. O próximo trabalho de produto é a
integração com o workflow `game_analysis`, não uma nova camada de
infraestrutura.

## Checklist de aceite

- [ ] Ubuntu 24.04 atualizado e timezone definido
- [ ] `nvidia-smi` e teste `docker --gpus all` funcionando
- [ ] Docker Engine, Buildx e Compose plugin instalados
- [ ] seis repositórios clonados e com `git status` compreensível
- [ ] `.env` criado sem segredos versionados
- [ ] `doctor` sem erro e instalação `FRESH`/`EXISTING` coerente
- [ ] `up --build`, migrations e todos os health checks OK
- [ ] Qwen `/health/ready` OK e referência carregada
- [ ] smoke funcional executado com fakes ou mídia curta controlada
- [ ] backups fora do Git e volumes preservados
- [ ] `down` deixa zero containers NeoVids ativos


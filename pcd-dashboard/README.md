# Monitor de PCDs — GOES DCS

Dashboard local para consultar, via protocolo **DDS** (DCP Data Service), o
status das suas PCDs cadastradas em um servidor **LRGS** da NOAA
(sete servidores públicos conhecidos, porta 16003 — veja lista abaixo).

Para cada endereço de PCD informado, mostra:
- Horário da última transmissão recebida (UTC)
- Há quanto tempo (idade da mensagem)
- Indicador de qualidade do dado (Normal / Razoável / Ruim)
- Força de sinal e canal GOES
- Sinalização automática de **"sem transmitir"** quando a última mensagem
  ultrapassa o limite configurado (padrão: 6h — ajustável na tela)

O cliente DDS (`dds_client.py`) é uma implementação direta do protocolo
binário sobre TCP, feita a partir da especificação oficial
*"DCP Data Service (DDS) Protocol Specification, Version 14"* (NOAA/NESDIS
& Cove Software). **Não depende do OpenDCS/Java.**

## Quatro abas

**Status atual**, **Percentual de transmissão** e **Teste de campo** — para
as PCDs que transmitem via satélite GOES (protocolo DDS/LRGS). Veja a
descrição de cada uma mais abaixo.

**PCDs Celular (FTP)** — para as 49 PCDs pluviométricas que transmitem via
rede de celular (modelo DualBase) e depositam os dados num servidor FTP
(`webfiles.inema.ba.gov.br`), em vez de satélite GOES. Estrutura real do
FTP (confirmada por inspeção manual):

```
/FTPCONSULTA/PCD-PLU/
    PG-PR-03/                          <- uma subpasta por estação,
    VJ-PR-19/                             nomeada com o código INEMA
    ...
        PG-PR-03_TABELA_2608061900.txt <- um arquivo por hora (AAMMDDHHmm)
        PG-PR-03_TABELA_2608062000.txt
        ...
```

Cada arquivo tem várias linhas, uma leitura a cada ~10 minutos, com a
data/hora real já na própria linha (`"2026-08-06 18:10:00","PG-PR-03",...`).
A aplicação usa **essa data do conteúdo** (não a data de modificação do
arquivo, nem só o nome) como referência de última transmissão — é a mais
precisa. Ela assume que esse horário está em hora local do Brasil (BRT,
UTC-3) e converte pra UTC internamente, no mesmo padrão usado em todo o
resto do app.

Tem duas sub-abas, iguais em espírito às do GOES: **Status atual** (última
leitura de cada estação, com indicador de sem transmissão) e **Percentual
de transmissão** (intervalo de datas configurável, calcula quantas leituras
deveria ter recebido vs. quantas recebeu de fato). Também tem um botão
**"Explorar arquivos brutos do FTP"** pra ver a listagem real do diretório
— útil se a correspondência por código não bater com alguma estação.

### Detalhando as três abas de GOES

**Status atual** — para cada PCD, mostra a última transmissão recebida, há
quanto tempo, qualidade do sinal, a última mensagem de dados transmitida, e
sinaliza quem está sem transmitir (inclusive quem não teve nenhum dado no
período — também conta como sem transmissão).

**Percentual de transmissão** — você escolhe um intervalo de datas/horários
(em UTC) e a aplicação calcula, para cada PCD, quantas transmissões deveria
ter recebido (assumindo o intervalo informado — 1h por padrão, já que suas
estações transmitem de hora em hora) vs. quantas efetivamente recebeu,
mostrando o percentual. Reenvios na mesma janela horária contam como 1 só,
para não inflar o percentual. Também mostra um indicador de **quantas
estações estão transmitindo agora** (definindo quantas horas contam como
"recente" num campo à parte).

**Teste de campo** — testa se uma PCD específica está transmitindo, na
hora. Escolha a estação numa lista (com nome/local) ou digite o endereço
diretamente, se for uma PCD nova que ainda não está cadastrada. Mostra um
indicador grande de "transmitindo" ou "sem transmissão", os dados da
estação e o histórico completo de mensagens recebidas na janela escolhida
(não só a última) — útil para verificar em campo se uma instalação nova ou
consertada está mandando dado de verdade.

Em todas as abas, cada linha da tabela também traz **Município**, **Nome da
estação** e **Código INEMA**, vindos de `stations.json` (GOES) ou
`stations_ftp.json` (celular) — arquivos estáticos dentro do projeto (não
editáveis pela interface; ver seção "Atualizar a lista de estações"
abaixo).

## Selecionar PCDs

- **Uma por uma**: cole os endereços na caixa de texto (um por linha).
- **Por tipo**: escolha uma categoria no menu (Meteorológica Hobeco,
  Meteorológica Campbell, Pluviométrica Campbell, Hidrológica Hexis,
  Hidrológica Ativa oeste, Porto Sul, AIBA, ou Outras/barragens) e clique em
  "+ Adicionar" — os endereços dessa categoria são somados à lista atual
  sem apagar o que já tinha.
- **Todas de uma vez**: botão "Carregar as 126 PCDs do INEMA".
- **Limpar lista**: botão para começar do zero.

## Atualizar a lista de estações

Não existe mais uma tela de "gestão de estações" — foi removida de
propósito (ver caveat abaixo). Pra adicionar, editar ou remover uma
estação, edite direto os arquivos `stations.json` e `categories.json` na
pasta do projeto:

- **`stations.json`**: uma lista de objetos com `address`, `label`,
  `municipio`, `nome_estacao`, `cod_inema`. O `address` precisa ser
  hexadecimal de 8 dígitos, em maiúsculas. (PCDs GOES)
- **`categories.json`**: um dicionário `{"Nome da categoria": ["ENDEREÇO1", "ENDEREÇO2", ...]}`.
  Um endereço pode (ou não) aparecer em alguma categoria — isso só afeta o
  botão "+ Adicionar por tipo". (PCDs GOES)
- **`stations_ftp.json`**: uma lista de objetos com `cod_inema`,
  `nome_estacao`, `municipio`, `lat`, `lon`, `tipo`, `modelo`,
  `meio_transmissao`, `label`. (PCDs celular/FTP) O `cod_inema` é o que
  a aplicação procura no nome dos arquivos do FTP pra casar com cada
  estação — se o nome do arquivo no servidor não contiver esse código,
  a correspondência falha (use "Explorar arquivos brutos do FTP" pra
  conferir os nomes reais).

Depois de editar, reinicie o app (local) ou faça commit + push (se estiver
publicado no Render, o deploy automático já pega a mudança).

## Parar uma consulta em andamento

Se a consulta estiver demorando (rede lenta, servidor sem resposta, lista
grande de PCDs), clique em **"Parar consulta"** — o botão aparece no lugar
de "Consultar plataformas"/"Calcular percentual" assim que a busca começa.
Isso interrompe a conexão com o LRGS imediatamente, tanto no navegador
quanto no servidor.

## Publicar online (Render, grátis, sem cartão)

O projeto já vem pronto pra isso (`Procfile`, `render.yaml`, `gunicorn`).

1. **Suba o código pro GitHub** (crie um repositório novo e faça push da pasta `pcd-dashboard`).
2. Acesse **[render.com](https://render.com)** → crie conta (sem cartão) → **New → Web Service**.
3. Conecte o repositório que você acabou de criar.
4. Render detecta o `render.yaml` automaticamente e sugere:
   - Build command: `pip install -r requirements.txt`
   - Start command: `gunicorn --workers 1 --threads 8 app:app`
5. Antes de confirmar, defina a variável de ambiente **`APP_PASSWORD`** com uma senha forte — é ela que protege o link público (sem isso, qualquer pessoa com a URL poderia usar suas credenciais da NOAA pelo formulário). A variável `SECRET_KEY` é gerada automaticamente pelo blueprint.
6. Clique em **Create Web Service**. Em alguns minutos você recebe uma URL tipo `https://pcd-dashboard-xxxx.onrender.com`.

**Sobre o tier gratuito do Render**: o serviço "dorme" após 15 minutos sem uso, e a primeira requisição depois disso demora uns 30-60s pra acordar (normal, não é bug). Fora isso funciona igual à versão local.

**Login do dashboard**: com `APP_PASSWORD` definida, a página inicial pede essa senha antes de liberar o acesso. É uma senha só sua/da equipe — separada do usuário/senha da NOAA, que continuam sendo digitados no formulário a cada consulta e nunca ficam salvos no servidor.

**Sem problema de persistência**: a única parte do app que gravava dados no
servidor (a antiga aba de gestão de estações) foi removida — hoje o app só
lê arquivos que já vêm no repositório (`stations.json`, `categories.json`)
e conversa com o LRGS da NOAA, sem gravar nada em disco. Isso significa que
funciona 100% no tier gratuito do Render sem nenhuma ressalva de "os dados
somem no próximo deploy". Pra atualizar a lista de estações, edite os
arquivos e faça um novo commit (ver "Atualizar a lista de estações" acima).

**Importante sobre "Parar consulta" em produção**: o botão de abortar depende de memória compartilhada dentro de UM processo — por isso o comando de start usa `--workers 1 --threads 8` (um processo só, várias threads). Não aumente o número de `--workers` sem ajustar esse mecanismo, senão o botão "Parar" pode não encontrar a consulta que quer interromper.

Se preferir outra plataforma (Railway, Fly.io, um VPS próprio), o `Procfile` e o `requirements.txt` já servem — só ajustar o comando de start conforme a plataforma escolhida (mantendo 1 worker).

## Iniciar sem digitar comandos

O projeto já vem com scripts de duplo clique que cuidam de tudo (checam o
Python, instalam as dependências e abrem o navegador sozinhos):

| Sistema | Arquivo | Como usar |
|---|---|---|
| Windows | `iniciar_windows.bat` | Dê duplo clique |
| macOS | `iniciar_mac.command` | Dê duplo clique (na primeira vez, clique com o botão direito → Abrir, pra liberar o macOS a rodar) |
| Linux | `iniciar_linux.sh` | Duplo clique (se o gerenciador de arquivos pedir) ou `./iniciar_linux.sh` no terminal |

Todos eles: verificam se o Python está instalado, instalam Flask/gunicorn
automaticamente, sobem o servidor e abrem `http://localhost:5000` no
navegador depois de alguns segundos. Pra encerrar, é só fechar a janela do
terminal que abriu (ou apertar Ctrl+C nela).

## Como rodar localmente (manual)

```bash
cd pcd-dashboard
pip install -r requirements.txt
python app.py
```

Abra **http://localhost:5000** no navegador.

Preencha:
- **Servidor LRGS**: um dos sete servidores públicos conhecidos:
  - `cdadata.wcda.noaa.gov`
  - `cdabackup.wcda.noaa.gov`
  - `nlrgs1.noaa.gov`
  - `nlrgs2.noaa.gov`
  - `lrgseddn1.cr.usgs.gov`
  - `lrgseddn2.cr.usgs.gov`

  Nem toda conta está provisionada em todos os servidores — se um deles der
  "Bad password" mesmo com a senha certa, tente outro da lista.
- **Usuário/senha DDS**: as credenciais que você já tem cadastradas
- **Endereços DCP**: um endereço hex de 8 dígitos por linha
- **Janela de busca**: quanto tempo para trás procurar mensagens (ex: 48h)
- **Alerta sem transmitir**: a partir de quantas horas sem mensagem uma PCD
  deve ser marcada como problema

## Notas importantes

- **Rede**: a máquina que roda este app precisa ter saída liberada para a
  porta TCP **16003** no host do LRGS. Se sua rede/firewall bloquear essa
  porta, a consulta vai falhar com erro de conexão.
- **Credenciais**: nunca são gravadas em disco. Vão do navegador para este
  servidor Flask local, e deste diretamente para a NOAA, por sessão. O
  campo de usuário já vem preenchido com "inema1" por conveniência, mas a
  senha **não** foi embutida no código por segurança — digite-a na tela a
  cada uso.
- **Autenticação**: o cliente tenta primeiro SHA-1; se o servidor exigir
  SHA-256 (protocolo v14), ele reconecta automaticamente com SHA-256.
- **Limite de threshold**: como combinado, deixei o limite de "sem
  transmitir" configurável na própria tela (campo "Alerta sem transmitir"),
  já que cada PCD pode ter um intervalo de transmissão esperado diferente.
  Se quiser, no futuro dá para evoluir isso para um limite por PCD (lendo,
  por exemplo, o intervalo de time-slot de cada uma a partir da PDT).
- Este projeto não é um produto oficial da NOAA/NESDIS.

## Estrutura

```
pcd-dashboard/
├── app.py                 # Backend Flask (rotas + orquestração)
├── dds_client.py           # Cliente do protocolo DDS/LRGS (PCDs GOES)
├── ftp_client.py            # Cliente FTP (PCDs celular)
├── stations.json            # Lista das 126 PCDs GOES
├── categories.json          # Categorias/tipos das PCDs GOES
├── stations_ftp.json        # Lista das 49 PCDs celulares (FTP)
├── templates/
│   ├── index.html          # Dashboard (HTML/CSS/JS, sem build step)
│   └── login.html          # Tela de login (se APP_PASSWORD estiver definida)
├── requirements.txt
├── Procfile                # Comando de start pra produção (Render etc.)
├── render.yaml              # Blueprint de deploy do Render
└── README.md
```

## Testado

`dds_client.py` foi testado localmente (framing de mensagens, geração do
autenticador SHA-1/SHA-256, parsing do cabeçalho DOMSAT de 37 bytes) contra
casos sintéticos batendo com a especificação, e depois validado contra o
servidor real da NOAA (bug de autenticação SHA-256 encontrado e corrigido
em conversa).

`ftp_client.py` foi testado contra um servidor FTP local simulando a
estrutura real do `webfiles.inema.ba.gov.br` (pasta por estação, arquivo
por hora, conteúdo idêntico ao de um arquivo real que foi conferido
manualmente) — parsing de nomes de arquivo, correspondência de pasta por
código INEMA, conversão de horário local (BRT) para UTC, e o mecanismo de
abortar consulta, tudo com testes automatizados passando. A conexão com o
servidor FTP real da INEMA **não pôde ser testada neste ambiente** (sem
acesso de rede a esse host neste sandbox) — teste primeiro com poucas
estações antes de rodar a consulta completa, e use o botão "Explorar
arquivos brutos do FTP" se alguma correspondência não bater.

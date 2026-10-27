# defects4j-bug-info

Ferramenta em Python para coleta e análise de informações sobre bugs em projetos Java, com suporte ao benchmark [Defects4J](https://github.com/rjust/defects4j).

O projeto automatiza a coleta de metadados de bugs, a análise das diferenças entre versões do código-fonte, a identificação de métodos afetados por meio de AST (*Abstract Syntax Tree*) e a geração de relatórios estruturados em JSON.

## Funcionalidades

- **Coleta de metadados:** obtém informações sobre o projeto e o bug informado no Defects4J.
- **Versões do código-fonte:** realiza checkout das versões com defeito (*buggy*) e corrigida (*fixed*).
- **Análise de diferenças:** identifica arquivos, linhas e alterações entre as versões.
- **Identificação de métodos:** utiliza análise sintática baseada em AST para relacionar linhas modificadas aos métodos Java correspondentes.
- **Seleção do método focal:** utiliza informações das alterações e dos testes de reprodução para selecionar um possível método principal relacionado ao bug.
- **Informações de reprodução:** coleta dados dos testes associados ao bug, incluindo informações de execução e rastreamento de pilha (*stack trace*), quando disponíveis.
- **Informações temporais:** permite buscar datas relacionadas ao relatório do bug e consultar o histórico Git do método.
- **Relatórios JSON:** organiza os dados coletados em arquivos que podem ser utilizados em análises posteriores.
- **Análise de projetos próprios:** permite localizar métodos em projetos Java a partir de uma linha do código-fonte ou de um arquivo de stack trace, sem exigir uma comparação entre versões com e sem defeito.

## Tecnologias utilizadas

- Python 3
- Java
- [Defects4J](https://github.com/rjust/defects4j)
- Git
- Tree-sitter
- Tree-sitter Java
- Perl

## Requisitos

Antes de executar a ferramenta, verifique se o ambiente possui:

1. Python 3 instalado.
2. Java compatível com a versão do Defects4J utilizada — Java 11 é uma referência para a configuração deste projeto, mas a compatibilidade pode variar conforme o projeto analisado.
3. Git e Perl instalados.
4. Defects4J instalado e inicializado.
5. As bibliotecas Python `tree-sitter` e `tree-sitter-java`.
6. Acesso ao repositório do projeto que será analisado e espaço em disco para os checkouts.

## Instalação

### 1. Clone o repositório

```bash
git clone <URL_DO_REPOSITORIO>
cd <NOME_DO_REPOSITORIO>
```

Substitua os valores entre `< >` pelo endereço e nome reais do repositório no GitHub.

### 2. Crie um ambiente virtual

```bash
python3 -m venv d4j-venv
```

Ative o ambiente:

**Linux:**

```bash
source d4j-venv/bin/activate
```

**Windows PowerShell:**

```powershell
.\d4j-venv\Scripts\Activate.ps1
```

### 3. Instale as dependências Python

```bash
python3 -m pip install tree-sitter tree-sitter-java
```

### 4. Configure o Defects4J

Instale e inicialize o Defects4J conforme as instruções do [repositório oficial](https://github.com/rjust/defects4j).

Verifique também se os comandos `git`, `perl` e `java` estão disponíveis no terminal.

Se necessário, informe explicitamente o caminho da instalação do Defects4J usando a opção `--d4j-home`.

## Como utilizar

### 1. Analisar um bug do Defects4J

O identificador do bug pode ser informado no formato `Projeto-ID`.

```bash
python3 d4j_bug_info.py Math-1
```

Esse comando inicia a coleta das informações disponíveis para o bug `1` do projeto `Math`.

O fluxo geral de análise é:

1. Interpretar o projeto e o identificador do bug.
2. Obter os metadados disponíveis.
3. Preparar os checkouts das versões com defeito e corrigida.
4. Identificar as diferenças entre os códigos-fonte.
5. Relacionar as linhas alteradas aos métodos Java usando AST.
6. Analisar os testes de reprodução e as informações de stack trace disponíveis.
7. Selecionar um método focal candidato.
8. Gerar um relatório JSON.

### 2. Definir o diretório de saída e incluir o código dos métodos

```bash
python3 d4j_bug_info.py Math 1 \
    --output ./dados \
    --with-source
```

O relatório será salvo no diretório `dados`, e a opção `--with-source` solicita a inclusão do código-fonte dos métodos nos resultados, conforme os recursos disponíveis na implementação.

### 3. Verificar a reprodução do bug

```bash
python3 d4j_bug_info.py Math-1 --verify
```

A opção `--verify` solicita a compilação e a execução dos testes de reprodução nas versões com defeito e corrigida. O resultado depende da configuração do ambiente e da disponibilidade dos testes necessários.

### 4. Consultar informações temporais

```bash
python3 d4j_bug_info.py Lang-1 \
    --fetch-report-dates \
    --method-history
```

- `--fetch-report-dates`: tenta obter datas relacionadas ao relatório do bug nas fontes externas compatíveis com a implementação.
- `--method-history`: tenta consultar o histórico Git associado ao método.

Essas informações podem não estar disponíveis para todos os projetos ou bugs. A obtenção de datas externas depende da existência de referências reconhecidas e do acesso às fontes consultadas.

### 6. Exibir o relatório no terminal

```bash
python3 d4j_bug_info.py Math-1 --stdout
```

A opção `--stdout` direciona o relatório JSON para a saída padrão, em vez de depender de um arquivo como saída principal.

## Análise de projetos Java próprios

Além do Defects4J, a ferramenta oferece um modo de análise de projetos Java locais.

Nesse modo, a identificação do método pode ser realizada sem comparar versões com defeito e corrigida. É necessário fornecer uma linha relevante do código-fonte ou informações de stack trace.

### 1. Localizar o método que contém uma linha

```bash
python3 d4j_bug_info.py \
    --project-path ./meu-projeto \
    --source-file src/main/java/p/Calculadora.java \
    --line 42
```

O comando procura o método Java que contém a linha 42 do arquivo indicado.

**Importante:** `./meu-projeto` é um exemplo. Substitua-o pelo caminho de um projeto Java que realmente exista no seu computador.

### 2. Analisar um arquivo de stack trace

```bash
python3 d4j_bug_info.py \
    --project-path ./meu-projeto \
    --stack-trace erro.log
```

A ferramenta utiliza os frames do stack trace para tentar localizar os métodos correspondentes no projeto.

Esse modo é útil quando se deseja analisar um projeto fora do Defects4J ou investigar um erro sem possuir uma versão corrigida para comparação.

## Identificação do método focal

A identificação do método focal é uma das etapas centrais do projeto.

No fluxo do Defects4J, a ferramenta utiliza as alterações entre as versões, os métodos associados às linhas modificadas e as informações dos testes de reprodução para selecionar um candidato.

Entre os critérios implementados estão:

1. **Método único afetado:** quando há apenas um método de produção candidato.
2. **Stack trace:** prioriza métodos relacionados aos frames dos testes de reprodução.
3. **Teste de reprodução:** utiliza informações sobre a classe de teste para restringir os candidatos.
4. **Quantidade de linhas alteradas:** considera métodos associados a um número maior de alterações.
5. **Ordem das alterações:** utiliza a ordem dos candidatos como critério final de desempate.

A escolha é heurística: o método focal indicado é um candidato relevante para a investigação, mas não constitui uma prova de que ele seja a causa raiz do defeito.

No modo de projeto próprio, a AST permite identificar qual método contém uma linha ou corresponde a um frame de stack trace. Entretanto, a AST, isoladamente, não determina se um método possui um bug.

## Saída gerada

Por padrão, a ferramenta salva um arquivo JSON no diretório de saída configurado.

Por exemplo, ao analisar `Math-1`, o arquivo esperado é:

```text
Math-1.json
```

Uma organização possível dos resultados inclui:

- Identificação do projeto e do bug.
- Referências às versões analisadas.
- Arquivos e linhas alterados.
- Métodos afetados.
- Método focal candidato e justificativa da seleção.
- Informações dos testes de reprodução.
- Informações temporais disponíveis.
- Histórico Git, quando solicitado e disponível.

A estrutura exata e a presença de cada campo dependem das informações obtidas e das opções utilizadas.

## Organização sugerida do repositório

```text
d4j-bug-info/
├── d4j_bug_info.py
├── README.md
├── requirements.txt
├── .gitignore
└── exemplos/
    ├── Math-1.json
    └── Lang-1.json
```

Essa estrutura é uma sugestão. Os relatórios de exemplo só devem ser incluídos se tiverem sido gerados e revisados; diretórios de trabalho, ambientes virtuais e checkouts dos projetos analisados não precisam fazer parte do repositório.

## Limitações conhecidas

- A seleção do método focal utiliza heurísticas e pode não identificar a causa raiz real do defeito.
- A identificação de métodos por AST depende de informações suficientes sobre o código-fonte.
- A análise de projetos próprios precisa de uma linha relevante ou de um stack trace; não é possível determinar automaticamente o método defeituoso apenas a partir da existência do projeto.
- A execução dos testes pode depender de versões específicas de Java, dependências e configurações de cada projeto.
- Datas de relatórios e histórico de métodos podem estar indisponíveis ou incompletos.
- A análise depende da disponibilidade dos repositórios e dos dados necessários para cada bug.

## Objetivo do projeto

O objetivo é automatizar a coleta e a organização de informações relevantes para a análise de defeitos em software Java, facilitando a investigação de bugs, a identificação de métodos potencialmente afetados e a realização de estudos sobre histórico, reprodução e correção de defeitos.

## Licença

Defina aqui a licença deste projeto, por exemplo, MIT, caso essa seja a licença escolhida. Antes de publicar, verifique também as condições de uso e distribuição das ferramentas e dos dados externos utilizados, incluindo o Defects4J.

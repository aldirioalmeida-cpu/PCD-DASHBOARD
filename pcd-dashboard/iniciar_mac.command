#!/bin/bash
cd "$(dirname "$0")"

echo "============================================"
echo "  Monitor de PCDs - GOES DCS"
echo "============================================"
echo

PYTHON_CMD=""
if command -v python3 &> /dev/null; then
    PYTHON_CMD="python3"
elif command -v python &> /dev/null; then
    PYTHON_CMD="python"
else
    echo "[ERRO] Python nao foi encontrado no seu computador."
    echo "Baixe e instale em: https://www.python.org/downloads"
    read -p "Pressione Enter para sair..."
    exit 1
fi

echo "Verificando/instalando dependencias, aguarde..."
$PYTHON_CMD -m pip install -r requirements.txt --quiet --disable-pip-version-check --break-system-packages 2>/dev/null \
  || $PYTHON_CMD -m pip install -r requirements.txt --quiet --disable-pip-version-check

echo
echo "Iniciando o servidor..."
echo "(Deixe esta janela aberta enquanto estiver usando o app)"
echo "Para encerrar, feche esta janela ou aperte Ctrl+C."
echo

( sleep 3 && open http://localhost:5000 ) &

$PYTHON_CMD app.py

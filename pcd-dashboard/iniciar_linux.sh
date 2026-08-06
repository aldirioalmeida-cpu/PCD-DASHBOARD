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
    echo "Instale com: sudo apt install python3 python3-pip  (Debian/Ubuntu)"
    read -p "Pressione Enter para sair..."
    exit 1
fi

echo "Verificando/instalando dependencias, aguarde..."
$PYTHON_CMD -m pip install -r requirements.txt --quiet --disable-pip-version-check --break-system-packages 2>/dev/null \
  || $PYTHON_CMD -m pip install -r requirements.txt --quiet --disable-pip-version-check

echo
echo "Iniciando o servidor..."
echo "(Deixe este terminal aberto enquanto estiver usando o app)"
echo "Para encerrar, feche o terminal ou aperte Ctrl+C."
echo

( sleep 3 && xdg-open http://localhost:5000 2>/dev/null ) &

$PYTHON_CMD app.py

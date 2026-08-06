@echo off
title Monitor de PCDs - GOES DCS
cd /d %~dp0

echo ============================================
echo   Monitor de PCDs - GOES DCS
echo ============================================
echo.

where python >nul 2>nul
if %errorlevel% neq 0 (
    echo [ERRO] Python nao foi encontrado no seu computador.
    echo Baixe e instale em: https://www.python.org/downloads
    echo Durante a instalacao, marque a opcao "Add Python to PATH".
    echo.
    pause
    exit /b 1
)

echo Verificando/instalando dependencias, aguarde...
python -m pip install -r requirements.txt --quiet --disable-pip-version-check
if %errorlevel% neq 0 (
    echo [ERRO] Falha ao instalar as dependencias.
    pause
    exit /b 1
)

echo.
echo Iniciando o servidor...
echo (Deixe esta janela aberta enquanto estiver usando o app)
echo Para encerrar, feche esta janela ou aperte Ctrl+C.
echo.

start "" cmd /c "timeout /t 3 /nobreak >nul && start http://localhost:5000"

python app.py

pause

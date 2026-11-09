@echo off
chcp 65001 >nul
title Painel dos bracos - TREINO (simulador)
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python nao foi encontrado neste computador.
    echo Peca ajuda: o guia explica como resolver.
    pause
    exit /b 1
)

echo ==============================================================
echo   MODO TREINO - NENHUM ROBO REAL SE MOVE
echo   Tres robos de mentirinha (simulador) + o painel.
echo.
echo   NAO FECHE esta janela nem a janela do simulador.
echo   As sequencias gravadas aqui ficam em arquivo separado
echo   (sequencias_simulador.json) e nao vao para os robos reais.
echo ==============================================================
echo.

start "Simulador dos robos (nao feche)" python simulador_robo.py --port 1025 --port 1026 --port 1027
timeout /t 2 /nobreak >nul

python painel_robos.py --open --robot esq=127.0.0.1:1025 --robot dir=127.0.0.1:1026 --robot centro=127.0.0.1:1027 --seq-file sequencias_simulador.json

echo.
echo O painel foi encerrado. Feche tambem a janela do simulador.
pause

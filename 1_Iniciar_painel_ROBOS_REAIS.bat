@echo off
chcp 65001 >nul
title Painel dos bracos - ROBOS REAIS
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python nao foi encontrado neste computador.
    echo Peca ajuda: o guia explica como resolver.
    pause
    exit /b 1
)

echo ==============================================================
echo   PAINEL DOS BRACOS - ROBOS REAIS (3 robos)
echo   Robo esq    = 192.168.0.11
echo   Robo dir    = 192.168.0.12
echo   Robo centro = 192.168.0.14
echo.
echo   NAO FECHE esta janela enquanto estiver usando o painel.
echo   Para encerrar: feche esta janela.
echo   EMERGENCIA: sempre o botao fisico do robo.
echo ==============================================================
echo.

python painel_robos.py --open --robot esq=192.168.0.11 --robot dir=192.168.0.12 --robot centro=192.168.0.14

echo.
echo O painel foi encerrado.
pause

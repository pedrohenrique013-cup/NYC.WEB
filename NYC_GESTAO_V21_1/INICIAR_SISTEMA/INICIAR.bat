@echo off
setlocal
chcp 65001 >nul
title NYC Gestao V21.1 - Inicializacao
set "ROOT=%~dp0.."
for %%I in ("%ROOT%") do set "ROOT=%%~fI"
set "BACKEND=%ROOT%\backend"
set "VENV=%BACKEND%\.venv"
set "PYTHON="

if not exist "%BACKEND%\requirements.txt" (
  echo ERRO: a pasta backend nao foi encontrada junto com INICIAR_SISTEMA.
  pause
  exit /b 1
)

if exist "%VENV%\Scripts\python.exe" (
  "%VENV%\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info[:2] == (3,12) else 1)" >nul 2>&1
  if not errorlevel 1 set "PYTHON=%VENV%\Scripts\python.exe"
)

if not defined PYTHON (
  set "VENV=%BACKEND%\.venv_inicio"
  if exist "%VENV%\Scripts\python.exe" (
    "%VENV%\Scripts\python.exe" -c "import sys; sys.exit(0 if sys.version_info[:2] == (3,12) else 1)" >nul 2>&1
    if errorlevel 1 (
      echo ERRO: backend\.venv_inicio existe com outra versao do Python.
      echo Para preservar esse ambiente, ele nao sera apagado automaticamente.
      echo Renomeie essa pasta manualmente ou instale o Python 3.12 e tente de novo.
      pause
      exit /b 1
    )
    set "PYTHON=%VENV%\Scripts\python.exe"
  ) else (
    echo Preparando o Python 3.12...
    py -3.12 -m venv "%VENV%" >nul 2>&1
    if errorlevel 1 (
      python -c "import sys; sys.exit(0 if sys.version_info[:2] == (3,12) else 1)" >nul 2>&1
      if errorlevel 1 (
        echo ERRO: Python 3.12 nao encontrado. Instale Python 3.12 64 bits e tente novamente.
        pause
        exit /b 1
      )
      python -m venv "%VENV%"
      if errorlevel 1 (
        echo ERRO: nao foi possivel criar o ambiente Python.
        pause
        exit /b 1
      )
    )
    set "PYTHON=%VENV%\Scripts\python.exe"
  )
)

if not exist "%BACKEND%\.env" (
  copy "%BACKEND%\.env.example" "%BACKEND%\.env" >nul
  if errorlevel 1 (
    echo ERRO: nao foi possivel criar backend\.env a partir do exemplo.
    pause
    exit /b 1
  )
)

"%PYTHON%" -c "import fastapi,uvicorn,sqlalchemy,pydantic,jose,passlib,bcrypt,psycopg,openpyxl,multipart,dotenv,qrcode" >nul 2>&1
if errorlevel 1 (
  echo Instalando os componentes necessarios. Na primeira vez, mantenha a internet conectada.
  "%PYTHON%" -m pip install -r "%BACKEND%\requirements.txt"
  if errorlevel 1 (
    echo ERRO: nao foi possivel instalar os componentes. Confira a conexao e tente novamente.
    pause
    exit /b 1
  )
)

if not exist "%ROOT%\logs" mkdir "%ROOT%\logs"
if not exist "%ROOT%\dados\backups" mkdir "%ROOT%\dados\backups"
echo Abrindo a Central NYC Gestao V21.1. Mantenha esta janela aberta durante o uso.
"%PYTHON%" "%~dp0central.py"
if errorlevel 1 (
  echo A Central foi encerrada com erro. Consulte logs\api.log se o servidor chegou a iniciar.
  pause
)
endlocal

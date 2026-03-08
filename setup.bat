@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul

echo.
echo   ╔════════════════════════════════════════╗
echo   ║   CLIP Assistant — Setup (Windows)    ║
echo   ╚════════════════════════════════════════╝
echo.

set "SCRIPT_DIR=%~dp0"
set "LLM_DIR=%~dp0LLM"
set "ERRORS=0"

:: ─── 1. Verificar winget ────────────────────────────────────────────────────
where winget >nul 2>&1
if %ERRORLEVEL% neq 0 (
  echo [!] winget nao encontrado.
  echo     Instala o "App Installer" na Microsoft Store e volta a correr este script.
  pause
  exit /b 1
)
echo [✓] winget disponivel

:: ─── 2. Docker Desktop ──────────────────────────────────────────────────────
echo.
echo ▶ Verificar Docker Desktop
where docker >nul 2>&1
if %ERRORLEVEL% equ 0 (
  echo [✓] Docker ja instalado
) else (
  echo [!] Docker Desktop nao encontrado.
  echo.
  echo     O Docker Desktop no Windows requer WSL2 e nao pode ser instalado
  echo     totalmente de forma automatica.
  echo.
  echo     Passos manuais:
  echo       1. Vai a: https://www.docker.com/products/docker-desktop/
  echo       2. Descarrega e instala o Docker Desktop para Windows
  echo       3. Ativa WSL2 quando pedido
  echo       4. Reinicia o computador se necessario
  echo       5. Abre o Docker Desktop e aguarda ate estar a correr
  echo       6. Volta a correr este script
  echo.
  set "ERRORS=1"
)

:: ─── 3. Ollama ───────────────────────────────────────────────────────────────
echo.
echo ▶ Verificar Ollama
where ollama >nul 2>&1
if %ERRORLEVEL% equ 0 (
  echo [✓] Ollama ja instalado
) else (
  echo [!] A instalar Ollama via winget...
  winget install --id Ollama.Ollama -e --silent
  if !ERRORLEVEL! equ 0 (
    echo [✓] Ollama instalado
  ) else (
    echo [✗] Falha ao instalar Ollama. Instala manualmente: https://ollama.com/download/windows
    set "ERRORS=1"
  )
)

:: ─── 4. Python 3 ─────────────────────────────────────────────────────────────
echo.
echo ▶ Verificar Python 3
where python >nul 2>&1
if %ERRORLEVEL% equ 0 (
  echo [✓] Python disponivel
) else (
  echo [!] A instalar Python 3 via winget...
  winget install --id Python.Python.3.12 -e --silent
  if !ERRORLEVEL! equ 0 (
    echo [✓] Python instalado ^(pode ser necessario reiniciar o terminal^)
  ) else (
    echo [✗] Falha ao instalar Python. Instala manualmente: https://www.python.org/downloads/
    set "ERRORS=1"
  )
)

:: ─── 6. Modelo Tejo ─────────────────────────────────────────────────────────
echo.
echo ▶ Configurar modelo Tejo
ollama show tejo >nul 2>&1
if %ERRORLEVEL% equ 0 (
  echo [✓] Modelo tejo ja existe - a regenerar para garantir actualizacao...
) else (
  echo [!] Modelo tejo nao encontrado - a criar...
)

:: Arrancar Ollama temporariamente se necessario
tasklist /fi "imagename eq ollama.exe" 2>nul | find /i "ollama.exe" >nul
if %ERRORLEVEL% neq 0 (
  start /min "" ollama serve
  echo|set /p="    A aguardar Ollama"
  :wait_ollama_setup
  curl -sf http://localhost:11434/api/tags >nul 2>&1
  if %ERRORLEVEL% neq 0 (
    echo|set /p="."
    timeout /t 2 /nobreak >nul
    goto wait_ollama_setup
  )
  echo.
)

:: Descarregar modelo base se necessario
ollama show qwen2.5:7b >nul 2>&1
if %ERRORLEVEL% neq 0 (
  echo [!] A descarregar qwen2.5:7b ^(~4.7 GB, pode demorar^)...
  ollama pull qwen2.5:7b
) else (
  echo [✓] qwen2.5:7b ja presente
)

:: Gerar Modelfile e criar modelo tejo (sempre, para garantir actualizacao)
echo [!] A gerar Modelfile e a criar modelo tejo...
python "%LLM_DIR%\generate_modelfile.py"
ollama create tejo -f "%LLM_DIR%\Modelfile"
if !ERRORLEVEL! equ 0 (
  echo [✓] Modelo tejo criado/actualizado
) else (
  echo [✗] Falha ao criar modelo tejo.
  set "ERRORS=1"
)

:: Verificar sitemap
if not exist "%LLM_DIR%\data\sitemap.json" (
  echo [!] data\sitemap.json nao encontrado - o backend nao vai funcionar sem ele.
  echo     Depois do setup, corre: docker compose --profile scrape run scraper
)

:: ─── 7. Resultado ────────────────────────────────────────────────────────────
echo.
if "%ERRORS%"=="1" (
  echo [!] Alguns componentes precisam de instalacao manual ^(ver mensagens acima^).
  echo     Depois de os instalar, volta a correr este script.
) else (
  echo [✓] Todos os requisitos instalados!
  echo.
  echo     Para arrancar o servidor:
  echo       cd LLM ^&^& start.bat
  echo.
  echo     O backend fica disponivel em: http://localhost:8000
  echo     Configura este URL na extensao Chrome.
)
echo.
pause

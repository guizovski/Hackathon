@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul

echo.
echo   ╔════════════════════════════════════════╗
echo   ║   CLIP Assistant — Arrancar servidor  ║
echo   ╚════════════════════════════════════════╝
echo.

set "SCRIPT_DIR=%~dp0"

:: ─── 1. Verificar dependências ───────────────────────────────────────────────
where ollama >nul 2>&1 || (echo [x] ollama nao encontrado. Corre setup.bat primeiro. & pause & exit /b 1)
where docker  >nul 2>&1 || (echo [x] docker nao encontrado. Corre setup.bat primeiro. & pause & exit /b 1)
echo [✓] Dependencias OK

:: ─── 2. Iniciar Ollama ──────────────────────────────────────────────────────
tasklist /fi "imagename eq ollama.exe" 2>nul | find /i "ollama.exe" >nul
if %ERRORLEVEL% neq 0 (
  echo [✓] A iniciar Ollama...
  start /min "" ollama serve
)

echo|set /p="    A aguardar Ollama"
:wait_ollama
curl -sf http://localhost:11434/api/tags >nul 2>&1
if %ERRORLEVEL% neq 0 (
  echo|set /p="."
  timeout /t 2 /nobreak >nul
  goto wait_ollama
)
echo.
echo [✓] Ollama pronto

:: ─── 3. Verificar modelo Tejo ─────────────────────────────────────────────
ollama show tejo >nul 2>&1
if %ERRORLEVEL% neq 0 (
  echo [x] Modelo 'tejo' nao encontrado. Corre:
  echo     python generate_modelfile.py ^&^& ollama create tejo -f Modelfile
  pause & exit /b 1
)
echo [✓] Modelo tejo presente

:: ─── 3b. Verificar sitemap ─────────────────────────────────────────────────
if not exist "%SCRIPT_DIR%data\sitemap.json" (
  echo [x] data\sitemap.json nao encontrado. Corre o scraper primeiro:
  echo     docker compose --profile scrape run scraper
  pause & exit /b 1
)
echo [✓] Sitemap encontrado

:: ─── 4. Iniciar Backend Docker ──────────────────────────────────────────────
echo [✓] A iniciar backend Docker (rebuild)...
docker compose -f "%SCRIPT_DIR%docker-compose.yml" up -d --build backend
if %ERRORLEVEL% neq 0 (
  echo [x] Erro ao iniciar Docker. Verifica se o Docker Desktop esta a correr.
  pause & exit /b 1
)

echo|set /p="    A aguardar backend"
set /a TRIES=0
:wait_backend
curl -sf http://localhost:8000/health >nul 2>&1
if %ERRORLEVEL% equ 0 goto backend_ready
set /a TRIES+=1
if %TRIES% geq 30 (
  echo.
  echo [x] Backend nao respondeu. Verifica: docker logs clip_backend
  pause & exit /b 1
)
echo|set /p="."
timeout /t 2 /nobreak >nul
goto wait_backend
:backend_ready
echo.
echo [✓] Backend pronto em http://localhost:8000

:: ─── 5. Tunnel opcional (cloudflared) ───────────────────────────────────────
set "SERVER_URL=http://localhost:8000"
where cloudflared >nul 2>&1
if %ERRORLEVEL% equ 0 (
  echo [✓] cloudflared encontrado - a iniciar tunnel publico...
  if exist "%TEMP%\cloudflared.log" del "%TEMP%\cloudflared.log"
  start /min "" cmd /c "cloudflared tunnel --url http://127.0.0.1:8000 --protocol http2 > %TEMP%\cloudflared.log 2>&1"
  echo|set /p="    A aguardar URL do tunnel"
  set /a CTRIES=0
  :wait_tunnel
  timeout /t 2 /nobreak >nul
  set /a CTRIES+=1
  for /f "delims=" %%U in ('powershell -Command "Select-String -Path '%TEMP%\cloudflared.log' -Pattern 'https://[a-zA-Z0-9-]+\.trycloudflare\.com' | ForEach-Object { $_.Matches[0].Value } | Select-Object -First 1" 2^>nul') do set "SERVER_URL=%%U"
  if "!SERVER_URL!"=="http://localhost:8000" (
    if %CTRIES% lss 30 (
      echo|set /p="."
      goto wait_tunnel
    )
  )
  echo.
)

echo.
echo   +---------------------------------------------+
echo   ^|  URL: !SERVER_URL!
echo   ^|  Configura este URL na extensao Chrome      ^|
echo   +---------------------------------------------+
echo.
echo [✓] Tudo a correr. Fecha esta janela para terminar os servicos.
echo.
pause

:: Cleanup
docker compose -f "%SCRIPT_DIR%docker-compose.yml" stop backend >nul 2>&1
taskkill /f /im cloudflared.exe >nul 2>&1
taskkill /f /im ollama.exe >nul 2>&1
echo [✓] Servicos terminados.

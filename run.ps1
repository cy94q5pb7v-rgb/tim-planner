# Локальный запуск ТИМ Планера (Windows PowerShell) без Docker.
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Test-Path .venv)) {
  Write-Host "-> создаю виртуальное окружение .venv"
  python -m venv .venv
  .\.venv\Scripts\python.exe -m pip install --upgrade pip | Out-Null
  .\.venv\Scripts\python.exe -m pip install -r requirements.txt
}

if (-not (Test-Path .env)) {
  Write-Host "-> .env не найден: копирую из .env.example (задайте PLANNER_SECRET)"
  Copy-Item .env.example .env
}

# подхватить переменные из .env
Get-Content .env | Where-Object { $_ -match '^\s*[^#].*=' } | ForEach-Object {
  $k, $v = $_ -split '=', 2
  [Environment]::SetEnvironmentVariable($k.Trim(), $v.Trim(), "Process")
}

Set-Location backend
..\.venv\Scripts\python.exe -m uvicorn web_app:app --host 127.0.0.1 --port 8000 --workers 1

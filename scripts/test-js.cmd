@echo off
REM Обёртка для запуска фронт-тестов из cmd / двойным кликом.
REM Реальная логика — в test-js.ps1 (portable Node из .tools/node или системный).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0test-js.ps1" %*

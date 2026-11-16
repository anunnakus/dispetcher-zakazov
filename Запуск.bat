@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "ROOT=%~dp0"
set "PY=%ROOT%runtime\python.exe"
set "CERT=%ROOT%runtime\cacert.pem"
set "SSL_CERT_FILE=%CERT%"
set "EMBED_URL=https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip"
set "CERT_URL=https://curl.se/ca/cacert.pem"

if exist "%PY%" goto cert
echo Первый запуск. Скачиваю портативный Python в папку программы.
echo Права администратора не нужны, в систему ничего не устанавливается.
if not exist "%ROOT%runtime" mkdir "%ROOT%runtime"
curl.exe -L --fail --retry 2 -o "%TEMP%\dispetcher-python-embed.zip" "%EMBED_URL%"
if errorlevel 1 goto download_failed
tar.exe -xf "%TEMP%\dispetcher-python-embed.zip" -C "%ROOT%runtime"
if errorlevel 1 goto download_failed
del "%TEMP%\dispetcher-python-embed.zip" >nul 2>&1
if not exist "%PY%" goto download_failed

:cert
if exist "%CERT%" goto run
echo Скачиваю файл сертификатов для почты, SMS и Telegram.
curl.exe -L --fail --retry 2 -o "%CERT%" "%CERT_URL%"
if errorlevel 1 (
  echo Не удалось скачать сертификаты. Почта и SMS могут не отправиться, сама программа откроется.
)

:run
if not exist "%PY%" goto try_system
"%PY%" "%ROOT%server.py"
set "CODE=%ERRORLEVEL%"
goto done

:try_system
where py >nul 2>&1
if not errorlevel 1 (
  py -3 "%ROOT%server.py"
  set "CODE=%ERRORLEVEL%"
  goto done
)
where python >nul 2>&1
if not errorlevel 1 (
  python "%ROOT%server.py"
  set "CODE=%ERRORLEVEL%"
  goto done
)
goto download_failed

:download_failed
echo.
echo Не получилось скачать Python. Офис мог закрыть python.org.
echo Принесите на флешке архив:
echo %EMBED_URL%
echo Распакуйте его содержимое в папку runtime рядом с этим файлом.
echo Рядом с python.exe положите cacert.pem со страницы:
echo %CERT_URL%
echo После этого снова откройте Запуск.bat.
set "CODE=1"

:done
echo.
echo Программа остановлена. Можно закрыть окно.
pause
exit /b %CODE%

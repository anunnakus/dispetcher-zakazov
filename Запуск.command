#!/bin/bash
cd "$(dirname "$0")"
if ! command -v python3 >/dev/null 2>&1; then
  echo "На этом компьютере нет Python 3."
  echo "Скачайте его с https://www.python.org/downloads/ и запустите файл ещё раз."
  read -r -p "Нажмите Enter, чтобы закрыть окно"
  exit 1
fi
echo "Открываю диспетчер заказов. Это окно не закрывайте, пока работаете."
python3 server.py
echo
read -r -p "Программа остановилась. Нажмите Enter, чтобы закрыть окно"

#!/bin/zsh
tg_contacts_project="${0:A:h}"
tg_contacts_install="$HOME/Library/Application Support/Telegram Contacts"
if [[ "$OSTYPE" == darwin* && -x "$tg_contacts_install/.venv/bin/python" ]]; then
    cd "$tg_contacts_install" || exit 1
else
    cd "$tg_contacts_project" || exit 1
fi
if [[ ! -x .venv/bin/python ]]; then
    print "Сначала установите программу по инструкции в README.md."
    exit 1
fi
exec .venv/bin/python app.py

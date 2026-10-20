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
if [[ "$OSTYPE" == darwin* && -r .local/python_arch ]]; then
    read -r tg_contacts_arch < .local/python_arch
    if [[ "$tg_contacts_arch" == arm64 || "$tg_contacts_arch" == x86_64 ]]; then
        exec /usr/bin/arch -"$tg_contacts_arch" .venv/bin/python app.py
    fi
fi
exec .venv/bin/python app.py

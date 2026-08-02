#!/bin/sh
set -eu

project_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
data_home=${XDG_DATA_HOME:-"$HOME/.local/share"}
app_dir="$data_home/flash-deck"
desktop_dir="$data_home/applications"
icon_dir="$data_home/icons/hicolor/scalable/apps"

mkdir -p "$app_dir" "$desktop_dir" "$icon_dir"
install -m 755 "$project_dir/stm32-flash-deck.py" "$app_dir/stm32-flash-deck.py"
install -m 644 "$project_dir/data/io.github.jrlomas.FlashDeck.svg" \
  "$icon_dir/io.github.jrlomas.FlashDeck.svg"

sed "s|@APP_PATH@|$app_dir/stm32-flash-deck.py|g" \
  "$project_dir/data/io.github.jrlomas.FlashDeck.desktop.in" \
  > "$desktop_dir/io.github.jrlomas.FlashDeck.desktop"
chmod 644 "$desktop_dir/io.github.jrlomas.FlashDeck.desktop"

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$desktop_dir"
fi
if command -v gtk4-update-icon-cache >/dev/null 2>&1; then
  gtk4-update-icon-cache -f -t "$data_home/icons/hicolor" >/dev/null 2>&1 || true
fi

printf '%s\n' "Flash Deck installed. Launch it from the Ubuntu application menu."

#!/usr/bin/env bash
cd "$(dirname "$0")" || exit 1

# 从桌面、应用菜单或文件管理器启动时，非登录 shell 的 PATH 通常不含
# nvm / npm-global / ~/.local/bin，先把这些常见目录补进去。
add_path() {
    [ -n "$1" ] && [ -d "$1" ] || return
    case ":$PATH:" in
        *":$1:"*) ;;
        *) PATH="$1:$PATH" ;;
    esac
}

# 多个 node 版本时取版本号最高的那个
node_bin=$(ls -d "$HOME"/.nvm/versions/node/*/bin 2>/dev/null | sort -V | tail -n 1)
add_path "$node_bin"
add_path "$HOME/.local/bin"
add_path "$HOME/.npm-global/bin"
add_path "$HOME/.bun/bin"
add_path "$HOME/.volta/bin"
export PATH
export QT_PLUGIN_PATH="/usr/lib/x86_64-linux-gnu/qt5/plugins"

exec python3 main.py "$@"

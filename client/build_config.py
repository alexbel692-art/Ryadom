#!/usr/bin/env python3
"""
Готовит параметры сборки клиента «Рядом» для workflow в .github/workflows/generator-*.yml.
Сервер берётся из переменных репозитория RYADOM_HOST/RYADOM_KEY/RYADOM_API, а если их нет — из client/server.json.

Логика перенесена из rdgen (https://github.com/bryangerlach/rdgen, rdgenerator/views.py,
GPL-3.0): то же, что генератор делает на своём сервере, здесь делается прямо в GitHub Actions.

  python3 client/build_config.py <platform> <host> <key> <api> <raw_base_url> <out_dir>

Создаёт <out_dir>/secrets.zip с secrets.json внутри (формат, который ждут workflow rdgen).
"""
import base64
import json
import os
import re
import sys
import uuid
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))


def build(params, platform, host, key, api, raw_base):
    p = params.get
    server = host or "rs-ny.rustdesk.com"
    serverPort = "21116"
    key = key or "OeVuKk5nlHiXp+APNn0Y3pC1Iwpwn44JGqrQCsWqmBw="
    apiServer = api or server + ":21114"
    urlLink = p("urlLink") or apiServer
    downloadLink = p("downloadLink") or apiServer.rstrip("/") + "/"
    direction = p("direction", "both")
    appname_display = p("appname") or "rustdesk"
    appname = re.sub(r"[:|\\/]", "", appname_display).replace(" ", "")
    filename = p("exename", "rustdesk")
    compname = (p("compname") or "Purslane Ltd").replace("&", "\\&")
    androidappid = p("androidappid") or "com.carriez.flutter_hbb"
    permPass = p("permanentPassword", "")
    theme, themeDorO = p("theme", "system"), p("themeDorO", "default")
    passApproveMode = p("passApproveMode", "password-click")
    hidecm = p("hidecm", False)
    removeNewVersionNotif = p("removeNewVersionNotif", False)

    if all(c.isascii() for c in filename):
        filename = re.sub(r"[^\w\s-]", "_", filename).strip().replace(" ", "_")
    else:
        filename = "rustdesk"
    if not all(c.isascii() for c in appname):
        appname = "rustdesk"

    custom = {}
    if direction != "Both":
        custom["conn-type"] = direction
    if p("installation") == "installationN":
        custom["disable-installation"] = "Y"
    if p("settings") == "settingsN":
        custom["disable-settings"] = "Y"
    if appname:
        custom["app-name"] = appname_display
    custom["override-settings"] = {}
    custom["default-settings"] = {}
    if permPass:
        custom["password"] = permPass
    if theme != "system":
        tgt = custom["default-settings" if themeDorO == "default" else "override-settings"]
        if platform == "windows-x86":
            tgt["allow-darktheme"] = "Y" if theme == "dark" else "N"
        else:
            tgt["theme"] = theme
    custom["enable-lan-discovery"] = "N" if p("denyLan", False) else "Y"
    custom["allow-auto-disconnect"] = "Y" if p("autoClose", False) else "N"

    yn = lambda name, default=True: "Y" if p(name, default) else "N"
    perms = custom["default-settings" if p("permissionsDorO", "default") == "default" else "override-settings"]
    perms.update({
        "access-mode": p("permissionsType", "custom"),
        "enable-keyboard": yn("enableKeyboard"),
        "enable-clipboard": yn("enableClipboard"),
        "enable-file-transfer": yn("enableFileTransfer"),
        "enable-audio": yn("enableAudio"),
        "enable-tunnel": yn("enableTCP"),
        "enable-remote-restart": yn("enableRemoteRestart"),
        "enable-record-session": yn("enableRecording"),
        "enable-block-input": yn("enableBlockingInput"),
        "allow-remote-config-modification": yn("enableRemoteModi", False),
        "direct-server": yn("enableDirectIP", False),
        "verification-method": "use-permanent-password" if hidecm else "use-both-passwords",
        "approve-mode": passApproveMode,
        "allow-hide-cm": "Y" if hidecm else "N",
        "allow-remove-wallpaper": yn("removeWallpaper"),
        "enable-remote-printer": yn("enablePrinter"),
        "enable-camera": yn("enableCamera"),
        "enable-terminal": yn("enableTerminal"),
    })
    if direction == "incoming":
        custom["override-settings"]["custom-rendezvous-server"] = server
        custom["override-settings"]["api-server"] = apiServer
    for field, section in (("defaultManual", "default-settings"), ("overrideManual", "override-settings")):
        for line in (p(field) or "").splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                custom[section][k.strip()] = v.strip()

    encoded = base64.b64encode(json.dumps(custom).encode("ascii")).decode("ascii")

    # Картинки workflow скачивает как  <url>/get_png?filename=..&uuid=..
    # Для raw.githubusercontent.com всё после «?» игнорируется — отдаётся сам файл.
    def link(name):
        if os.path.exists(os.path.join(HERE, name)):
            return f"{raw_base.rstrip('/')}/client/{name}?", "x", name
        return "false", "false", "false"

    icon, logo, privacy = link("icon.png"), link("logo.png"), link("privacy.png")
    return {
        "server": server, "serverPort": serverPort, "key": key, "apiServer": apiServer,
        "custom": encoded, "uuid": str(uuid.uuid4()),
        "iconlink_url": icon[0], "iconlink_uuid": icon[1], "iconlink_file": icon[2],
        "logolink_url": logo[0], "logolink_uuid": logo[1], "logolink_file": logo[2],
        "privacylink_url": privacy[0], "privacylink_uuid": privacy[1], "privacylink_file": privacy[2],
        "appname": appname, "genurl": "", "urlLink": urlLink, "downloadLink": downloadLink,
        "delayFix": "true" if p("delayFix", True) else "false",
        "rdgen": "ryadom",      # не 'true'/'false' → шаги отправки на сервер rdgen пропускаются
        "xOffline": "true" if p("xOffline", False) else "false",
        "removeNewVersionNotif": "true" if removeNewVersionNotif else "false",
        "update_public_key": "", "compname": compname, "androidappid": androidappid,
        "filename": filename, "token": "",
    }


def main():
    if len(sys.argv) != 7:
        sys.exit(__doc__)
    platform, host, key, api, raw_base, out = sys.argv[1:]
    with open(os.path.join(HERE, "ryadom.json"), encoding="utf-8") as f:
        params = {k: v for k, v in json.load(f).items() if not k.startswith("_")}
    try:
        with open(os.path.join(HERE, "server.json"), encoding="utf-8") as f:
            srv = json.load(f)
    except OSError:
        srv = {}
    host = host or srv.get("host", "")
    key = key or srv.get("key", "")
    api = api or srv.get("api", "")
    if not host or not key:
        print("::warning::Не заданы переменные RYADOM_HOST / RYADOM_KEY — клиент будет собран "
              "с публичным сервером RustDesk. Задайте их в Settings → Secrets and variables → Actions → Variables.")
    data = build(params, platform, host.strip(), key.strip(), api.strip(), raw_base)
    os.makedirs(out, exist_ok=True)
    with zipfile.ZipFile(os.path.join(out, "secrets.zip"), "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("secrets.json", json.dumps(data, ensure_ascii=False))
    shown = {k: data[k] for k in ("server", "apiServer", "key", "appname", "filename", "androidappid")}
    print(json.dumps(shown, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

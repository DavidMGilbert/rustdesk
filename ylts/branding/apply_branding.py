#!/usr/bin/env python3
"""Turn a checkout of github.com/rustdesk/rustdesk into the YLTS Remote build.

What it changes (each edit is anchored to exact upstream text and fails loudly if RustDesk
has changed underneath it, rather than silently producing a half-branded build):

1. libs/hbb_common/src/config.rs - default ID server, public key and app name.
2. src/common.rs - loads an unsigned `ylts.json` next to the exe. Upstream only accepts
   custom-client configs signed by RustDesk Ltd (their paid generator); this is the
   self-hosted equivalent. The file sits in Program Files, so only admins can change it.
3. Icons - replaces the app/tray icons with the YLTS set from branding/generated.

Usage (from the RustDesk checkout root, after `git submodule update --init`):
    python3 ../ylts-remote/branding/apply_branding.py --host remote.ylts.com.au --key <hbbs public key>
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
MARK = "YLTS-BRANDING"


def edit(path: Path, old: str, new: str, *, regex: bool = False, count: int = 1) -> None:
    text = path.read_text(encoding="utf-8")
    if MARK in new and new in text:
        return  # already applied
    if regex:
        result, n = re.subn(old, new, text, count=count)
    else:
        n = text.count(old)
        result = text.replace(old, new, count)
    if n != count:
        sys.exit(f"[branding] {path}: expected {count} match(es) for anchor, found {n}.\n"
                 f"  anchor: {old[:120]!r}\n  RustDesk probably changed; update apply_branding.py.")
    path.write_text(result, encoding="utf-8")
    print(f"[branding] patched {path}")


def patch_config(root: Path, host: str, key: str) -> None:
    cfg = root / "libs/hbb_common/src/config.rs"
    edit(cfg, r'pub const RENDEZVOUS_SERVERS: &\[&str\] = &\["[^"]*"\];',
         f'pub const RENDEZVOUS_SERVERS: &[&str] = &["{host}"]; // {MARK}', regex=True)
    edit(cfg, r'pub const RS_PUB_KEY: &str = "[^"]*";',
         f'pub const RS_PUB_KEY: &str = "{key}"; // {MARK}', regex=True)


LOADER = '''
// {mark}: unsigned custom-client config for self-hosted YLTS builds.
fn load_ylts_client_config() -> bool {{
    let Some(dir) = std::env::current_exe().ok().and_then(|x| x.parent().map(|x| x.to_path_buf())) else {{
        return false;
    }};
    let path = dir.join("ylts.json");
    let Ok(text) = std::fs::read_to_string(&path) else {{
        return false;
    }};
    match serde_json::from_str::<std::collections::HashMap<String, serde_json::Value>>(&text) {{
        Ok(data) => {{
            apply_custom_client_data(data);
            true
        }}
        Err(e) => {{
            log::error!("Invalid ylts.json: {{e}}");
            false
        }}
    }}
}}

pub fn load_custom_client() {{
    if load_ylts_client_config() {{
        return;
    }}'''


def patch_loader(root: Path) -> None:
    common = root / "src/common.rs"
    edit(common, "\npub fn load_custom_client() {", LOADER.format(mark=MARK))
    # Split read_custom_client after signature verification so both paths share the apply logic.
    edit(common, '    if let Some(app_name) = data.remove("app-name") {',
         f'    apply_custom_client_data(data);\n}}\n\n// {MARK}\n'
         'pub fn apply_custom_client_data(mut data: std::collections::HashMap<String, serde_json::Value>) {\n'
         '    if let Some(app_name) = data.remove("app-name") {')
    # read_custom_client declared `mut data`; after the split it is only moved.
    edit(common, "    let Ok(mut data) =\n        serde_json::from_slice::<std::collections::HashMap<String, serde_json::Value>>(&data)",
         f"    let Ok(data) = // {MARK}\n        serde_json::from_slice::<std::collections::HashMap<String, serde_json::Value>>(&data)")


def patch_ui(root: Path) -> None:
    """Replace the user-visible RustDesk name, copyright and website in the desktop app."""
    about = root / "flutter/lib/desktop/pages/desktop_setting_page.dart"
    edit(about, "child: _Card(title: translate('About RustDesk'), children: [",
         "child: _Card(title: 'About ${bind.mainGetAppNameSync()}', children: [ // " + MARK)
    edit(about, """              InkWell(
                  onTap: () {
                    launchUrlString('https://rustdesk.com/privacy.html');
                  },
                  child: Text(
                    translate('Privacy Statement'),
                    style: linkStyle,
                  ).marginSymmetric(vertical: 4.0)),
              InkWell(
                  onTap: () {
                    launchUrlString('https://rustdesk.com');
                  },
                  child: Text(
                    translate('Website'),
                    style: linkStyle,
                  ).marginSymmetric(vertical: 4.0)),""",
         """              SelectionArea(
                  child: const Text('Phone: 0483 866 665')
                      .marginSymmetric(vertical: 4.0)),
              InkWell(
                  onTap: () {
                    launchUrlString('mailto:hello@ylts.com.au');
                  },
                  child: const Text(
                    'hello@ylts.com.au',
                    style: linkStyle,
                  ).marginSymmetric(vertical: 4.0)),
              InkWell(
                  onTap: () {
                    launchUrlString('https://ylts.com.au');
                  },
                  child: const Text(
                    'ylts.com.au',
                    style: linkStyle,
                  ).marginSymmetric(vertical: 4.0)), // """ + MARK)
    edit(about,
         "'Copyright © ${DateTime.now().toString().substring(0, 4)} Purslane Tech Pte. Ltd.\\n$license',",
         "'Copyright © ${DateTime.now().toString().substring(0, 4)} Your Local Tech Solutions\\n$license', // " + MARK)
    title = root / "flutter/lib/desktop/widgets/tabbar_widget.dart"
    edit(title, """                            child: const Text(
                              "RustDesk",
                              style: TextStyle(fontSize: 13),
                            ).marginOnly(left: 2))""",
         """                            child: Text(
                              bind.mainGetAppNameSync(),
                              style: const TextStyle(fontSize: 13),
                            ).marginOnly(left: 2)) // """ + MARK)
    home = root / "flutter/lib/desktop/pages/desktop_home_page.dart"
    edit(home, """          if (isOutgoingOnly)
            Text(
              translate("outgoing_only_desk_tip"),
              overflow: TextOverflow.clip,
              style: Theme.of(context).textTheme.bodySmall,
            ),""",
         "          // " + MARK + "\n")
    logo = root / "flutter/lib/common.dart"
    edit(logo, "constraints: BoxConstraints(maxWidth: 300, maxHeight: 60),",
         "constraints: const BoxConstraints(maxWidth: 250, maxHeight: 88), // " + MARK)
    install = root / "src/platform/windows.rs"
    edit(install, """md \\"{path}\\"
{copy_exe}
reg add {subkey} /f""",
         """md \\"{path}\\"
{copy_exe}
{rename_exe}
reg add {subkey} /f""")
    edit(install, "        copy_exe = copy_exe_cmd(&src_exe, &exe, &path)?,\n        import_config = get_import_config(&exe),",
         "        copy_exe = copy_exe_cmd(&src_exe, &exe, &path)?,\n        rename_exe = rename_exe_cmd(&src_exe, &path)?, // " + MARK + "\n        import_config = get_import_config(&exe),")
    # Signed-in clients wait for hbbs to start a key exchange. The open-source
    # server never does, so the wait ends as "Failed to secure tcp: deadline has elapsed".
    edit(root / "src/common.rs",
         "    match timeout(READ_TIMEOUT, conn.next()).await? {\n        Some(Ok(bytes)) => {",
         "    let first = match timeout(1500, conn.next()).await {\n"
         "        Ok(msg) => msg,\n"
         "        Err(_) => return Ok(false), // " + MARK + "\n"
         "    };\n"
         "    match first {\n"
         "        Some(Ok(bytes)) => {")


def replace_icons(root: Path) -> None:
    gen = HERE / "generated"
    if not (gen / "ylts.ico").exists():
        sys.exit("[branding] run branding/make_icons.py first")
    targets = {
        "res/icon.ico": "ylts.ico",
        "res/tray-icon.ico": "ylts.ico",
        "flutter/windows/runner/resources/app_icon.ico": "ylts.ico",
        "res/icon.png": "icon-512.png",
        "res/128x128.png": "icon-128.png",
        "res/128x128@2x.png": "icon-256.png",
        "res/64x64.png": "icon-64.png",
        "res/32x32.png": "icon-32.png",
        "flutter/assets/icon.png": "icon-512.png",
    }
    for dst, src in targets.items():
        p = root / dst
        if p.exists():
            shutil.copyfile(gen / src, p)
            print(f"[branding] icon {dst}")
    assets = root / "flutter/assets"
    for name in ("logo.png", "logo_light.png", "logo_dark.png"):
        src = gen / name
        if not src.exists():
            sys.exit(f"[branding] missing {src}")
        shutil.copyfile(src, assets / name)
        print(f"[branding] wordmark {name}")
    svg = root / "flutter/assets/icon.svg"
    if svg.exists():
        shutil.copyfile(HERE.parent / "server/portal/app/static/icon.svg", svg)


def render_variants(host: str, key: str, out: Path) -> None:
    """Write ylts-<variant>.json with the server details filled in."""
    out.mkdir(parents=True, exist_ok=True)
    for src in sorted((HERE / "variants").glob("*.json")):
        text = src.read_text(encoding="utf-8").replace("{{HOST}}", host).replace("{{KEY}}", key)
        data = json.loads(text)  # validate
        # RustDesk 1.5 refuses to install unless the name is [A-Za-z0-9-]+. A space
        # makes --silent-install fail before any files are copied.
        app_name = data.get("app-name", "")
        if not isinstance(app_name, str) or not re.fullmatch(r"[A-Za-z0-9-]+", app_name):
            sys.exit(f"[branding] {src.name}: app-name must match [A-Za-z0-9-]+, got {app_name!r}")
        (out / f"ylts-{src.stem}.json").write_text(text, encoding="utf-8")
        print(f"[branding] variant {src.stem}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", required=True, help="ID/relay server host, e.g. remote.ylts.com.au")
    ap.add_argument("--key", required=True, help="contents of id_ed25519.pub from the server")
    ap.add_argument("--root", default=".", help="RustDesk checkout (default: current directory)")
    ap.add_argument("--variants-only", metavar="DIR", help="only render the variant configs into DIR")
    a = ap.parse_args()
    if a.variants_only:
        render_variants(a.host.strip(), a.key.strip(), Path(a.variants_only))
        return
    root = Path(a.root).resolve()
    if not (root / "libs/hbb_common/src/config.rs").exists():
        sys.exit("[branding] not a RustDesk checkout with submodules (libs/hbb_common missing)")
    if not re.fullmatch(r"[A-Za-z0-9+/=]{40,60}", a.key.strip()):
        sys.exit("[branding] --key doesn't look like an hbbs public key")
    patch_config(root, a.host.strip(), a.key.strip())
    patch_loader(root)
    patch_ui(root)
    replace_icons(root)
    print("[branding] done")


if __name__ == "__main__":
    main()

# Generates wix/main-installer.wxs from the PyInstaller output folder
# (dist/vlcspeaker: the exe, _internal\ and the copied piper\voices).
#
# Run from the repo root (after PyInstaller + copying the voices):
#     python wix/gen_installer.py
#     wix build -arch x64 -ext WixToolset.UIExtension ^
#         -o dist/VLCSpeaker-Setup-0.0.1.msi wix/main-installer.wxs
# (build_installer.cmd does all of this in order.)
#
# Component GUIDs are derived from each file's relative path, so rebuilding
# produces identical component identities, so upgrades/repairs stay clean.

import os
import sys
import uuid

VERSION = "0.0.1"
APP_NAME = "VLCspeaker"
APP_DIR_NAME = "VLCSpeaker"
UPGRADE_CODE = "8F9A2B3C-4D5E-6F7A-8B9C-0D1E2F3A4B5C"
APP_DIST = "dist/vlcspeaker"
OUT_WXS = "wix/main-installer.wxs"
BS = chr(92)  # backslash, kept out of string literals to avoid escape traps


def guid_for(key):
    return "{%s}" % str(uuid.uuid5(uuid.NAMESPACE_URL,
                                   "vlcspeaker-msi:" + key.replace(BS, "/"))).upper()


def id_for(prefix, rel):
    return prefix + "_" + uuid.uuid5(uuid.NAMESPACE_URL,
                                     prefix + ":" + rel.replace(BS, "/")).hex[:16]


comps = []          # component ids, in emit order
body = []           # wxs lines


def emit_file(rel, dir_id):
    cid, fid = id_for("c", rel), id_for("f", rel)
    comps.append(cid)
    body.append(f'            <Component Id="{cid}" Guid="{guid_for(rel)}" Directory="{dir_id}">')
    body.append(f'                <File Id="{fid}" Name="{os.path.basename(rel)}" '
                f'Source="{APP_DIST}/{rel}" KeyPath="yes" />')
    body.append('            </Component>')


def emit_tree(root, base_n, dir_id):
    """Recursively emit one component per file under root/base_n."""
    for entry in sorted(os.scandir(root), key=lambda e: e.name):
        rel = base_n + "/" + entry.name
        if entry.is_dir():
            cid = id_for("d", rel)
            body.append(f'            <Directory Id="{cid}" Name="{entry.name}">')
            emit_tree(entry.path, rel, cid)
            body.append('            </Directory>')
        elif entry.is_file():
            emit_file(rel, dir_id)
        else:
            print("skip (not a regular file):", rel, file=sys.stderr)


if not os.path.isdir(APP_DIST):
    sys.exit("error: %s not found - run PyInstaller first "
             "(build_installer.cmd step 1)" % APP_DIST)

voices = []
voices_root = os.path.join(APP_DIST, "piper", "voices")
if os.path.isdir(voices_root):
    voices = [f for f in sorted(os.listdir(voices_root)) if f.endswith(".onnx")]
if not voices:
    print("warning: no voice models in %s - the installed app will have no "
          "offline voice until one is downloaded" % voices_root, file=sys.stderr)

body.append('<?xml version="1.0" encoding="UTF-8"?>')
body.append('<Wix xmlns="http://wixtoolset.org/schemas/v4/wxs" '
            'xmlns:ui="http://wixtoolset.org/schemas/v4/wxs/ui">')
body.append(f'  <Package Name="{APP_NAME}" Manufacturer="{APP_NAME} Project" '
            f'Version="{VERSION}"')
body.append(f'           UpgradeCode="{UPGRADE_CODE}" Scope="perMachine" '
            'Compressed="yes" Language="1033" InstallerVersion="500">')
body.append('    <SummaryInformation Description="VLCspeaker - watch foreign-language '
            'movies by listening (installer)" />')
body.append('    <MajorUpgrade DowngradeErrorMessage="A newer version of '
            '[ProductName] is already installed." AllowSameVersionUpgrades="yes" />')
body.append('    <MediaTemplate EmbedCab="yes" CompressionLevel="high" />')
body.append('    <Icon Id="AppIco" SourceFile="assets\\icon.ico" />')
body.append('    <Property Id="ARPPRODUCTICON" Value="AppIco" />')

body.append('    <StandardDirectory Id="ProgramFiles64Folder">')
body.append(f'      <Directory Id="INSTALLFOLDER" Name="{APP_DIR_NAME}">')
# The app exe gets its own named component so repairs/upgrades stay precise
exe_rel = "vlcspeaker.exe"
cid, fid = id_for("c", exe_rel), id_for("f", exe_rel)
comps.append(cid)
body.append(f'        <Component Id="{cid}" Guid="{guid_for(exe_rel)}">')
body.append(f'          <File Id="{fid}" Name="vlcspeaker.exe" '
            f'Source="{APP_DIST}/{exe_rel}" KeyPath="yes" />')
body.append('        </Component>')
# Everything PyInstaller produced next to the exe (including piper\voices)
body.append('        <Directory Id="_INTERNAL" Name="_internal">')
emit_tree(os.path.join(APP_DIST, "_internal"), "_internal", "_INTERNAL")
body.append('        </Directory>')
# The offline neural voice(s), scanned by the app at APP_DIR\piper\voices
body.append('        <Directory Id="PIPER" Name="piper">')
body.append('          <Directory Id="VOICES" Name="voices">')
emit_tree(os.path.join(APP_DIST, "piper", "voices"), "piper/voices", "VOICES")
body.append('          </Directory>')
body.append('        </Directory>')
# Let Windows find the app for "Open with" / the Run dialog
body.append('        <Component Id="AppPaths" Guid="%s">' % guid_for("apppaths"))
body.append('          <RegistryValue Root="HKLM" '
            'Key="SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\App Paths\\vlcspeaker.exe" '
            'Value="[INSTALLFOLDER]vlcspeaker.exe" Type="string" KeyPath="yes" />')
body.append('        </Component>')
body.append('      </Directory>')
body.append('    </StandardDirectory>')

body.append('    <StandardDirectory Id="ProgramMenuFolder">')
body.append(f'      <Directory Id="AppMenuDir" Name="{APP_NAME}">')
body.append('        <Component Id="StartMenuShortcuts" Guid="%s">' % guid_for("startmenu"))
body.append('          <Shortcut Id="AppShortcut" Name="VLCspeaker" '
            'Description="Watch foreign-language movies by listening" '
            'Target="[INSTALLFOLDER]vlcspeaker.exe" WorkingDirectory="INSTALLFOLDER" '
            'Icon="AppIco" />')
body.append('          <Shortcut Id="UninstallShortcut" Name="Uninstall VLCspeaker" '
            'Description="Remove VLCspeaker" Target="[System64Folder]msiexec.exe" '
            'Arguments="/x [ProductCode]" />')
body.append('          <RemoveFolder Id="RemoveAppMenuDir" Directory="AppMenuDir" '
            'On="uninstall" />')
body.append('          <RegistryValue Root="HKCU" Key="Software\\VLCSpeaker" '
            'Name="StartMenuShortcut" Type="integer" Value="1" KeyPath="yes" />')
body.append('        </Component>')
body.append('      </Directory>')
body.append('    </StandardDirectory>')

body.append('    <StandardDirectory Id="DesktopFolder">')
body.append('      <Component Id="DesktopShortcut" Guid="%s">' % guid_for("desktop"))
body.append('        <Shortcut Id="DesktopShortcutLink" Name="VLCspeaker" '
            'Description="Watch foreign-language movies by listening" '
            'Target="[INSTALLFOLDER]vlcspeaker.exe" WorkingDirectory="INSTALLFOLDER" '
            'Icon="AppIco" />')
body.append('        <RegistryValue Root="HKCU" Key="Software\\VLCSpeaker" '
            'Name="DesktopShortcut" Type="integer" Value="1" KeyPath="yes" />')
body.append('      </Component>')
body.append('    </StandardDirectory>')

body.append('    <ui:WixUI Id="WixUI_InstallDir" InstallDirectory="INSTALLFOLDER" />')

body.append('    <Feature Id="Main" Level="1" Title="VLCspeaker" '
            'Description="Install VLCspeaker and the bundled Piper neural voice.">')
for cid in comps:
    body.append(f'      <ComponentRef Id="{cid}" />')
body.append('      <ComponentRef Id="AppPaths" />')
body.append('      <ComponentRef Id="StartMenuShortcuts" />')
body.append('      <ComponentRef Id="DesktopShortcut" />')
body.append('    </Feature>')

body.append('  </Package>')
body.append('</Wix>')

with open(OUT_WXS, "w", encoding="utf-8") as fh:
    fh.write("\n".join(body) + "\n")

disk = sum(os.path.getsize(os.path.join(r, f))
           for r, _, fs in os.walk(APP_DIST) for f in fs)
print("%s written: %d components, %.0f MB payload, %d voice(s)"
      % (OUT_WXS, len(comps), disk / 1e6, len(voices)))
